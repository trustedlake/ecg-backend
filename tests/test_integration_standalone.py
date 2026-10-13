"""Stand-alone Django service contract checks with mock settings, no DB/AWS required."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import importlib.util
import sys
import unittest

BACKEND = Path(__file__).resolve().parents[1]


def load_services():
    fake_django = ModuleType('django')
    fake_conf = ModuleType('django.conf')
    fake_conf.settings = SimpleNamespace(
        AWS_LAMBDA_ECG_ANALYSER='new-lambda',
        AWS_IMAGES_BUCKET='images', AWS_RESULTS_BUCKET='results',
        PRESIGN_URL_EXPIRES=3600, AWS_LAMBDA_READ_TIMEOUT=270,
    )
    with patch.dict(sys.modules, {'django':fake_django,'django.conf':fake_conf}):
        spec=importlib.util.spec_from_file_location('isolated_services', BACKEND/'exams/services.py')
        obj=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(obj)
    return obj

class MockAttempt:
    def __init__(self):
        self.id='7d2231fa-5662-41e1-852b-5a1a7fe7c072'
        self.imagem_s3_key='myuser/2026/input.png'
        self.calibracao={'speed_mm_s':50, 'gain_mm_mv':20}
        self.sinais={}
        self.mascara_s3_key=None
        self.overlay_trace_s3_key=None
        self.overlay_signal_s3_key=None
        self.saves=[]
    def save(self,**kw): self.saves.append(kw)

class ServiceContracts(unittest.TestCase):
    def setUp(self):
        self.s=load_services();self.a=MockAttempt();self.e=SimpleNamespace(id='35585ae9-4de8-4888-8ac4-23cf14319803')
        self.keys={k:'ecg-analyser/'+self.a.id+'/initial/'+name for k,name in
                   [('result','result.json'),('signals','signals.npz'),('trace_masks','trace_masks.npz'),
                    ('working_image','working_image.png'),('visao_geral','visao_geral.png')]}

    def test_forward_calibration_and_store_all_artifacts(self):
        with patch.object(self.s,'invoke_lambda',return_value={'status':'CONSISTENCY_PASS','leads':[{'name':'II'}],'artifacts':self.keys}) as invoke,\
             patch.object(self.s,'presign_get_url',return_value='signed'),\
             patch.object(self.s,'original_image_url',return_value='img'):
            result=self.s.run_phase_a(self.a,self.e)
        self.assertEqual(invoke.call_count,1)
        self.assertEqual(invoke.call_args.args[1]['calibration'],{'speed_mm_s':50,'gain_mm_mv':20})
        self.assertEqual(self.a.mascara_s3_key,self.keys['trace_masks'])
        self.assertEqual(self.a.sinais['working_image_s3_key'],self.keys['working_image'])
        self.assertEqual(self.a.calibracao, {'speed_mm_s':50, 'gain_mm_mv':20})
        self.assertEqual(result['analyser_status'],'CONSISTENCY_PASS')

    def test_no_calibration_no_lambda(self):
        self.a.calibracao=None
        with patch.object(self.s,'invoke_lambda') as invoke:
            with self.assertRaisesRegex(self.s.UpstreamError,'Calibração'):
                self.s.run_phase_a(self.a,self.e)
            invoke.assert_not_called()

    def test_no_missing_success_artifacts_accepted(self):
        invalid=dict(self.keys);invalid.pop('trace_masks')
        with patch.object(self.s,'invoke_lambda',return_value={'status':'CONSISTENCY_PASS','artifacts':invalid}):
            with self.assertRaisesRegex(self.s.UpstreamError,'artefatos'):
                self.s.run_phase_a(self.a,self.e)

    def test_rejected_without_signal(self):
        with patch.object(self.s,'invoke_lambda',return_value={'status':'REJECTED_LOW_TRACE_GRID_CONTRAST','artifacts':{'result':self.keys['result']},'leads':[]}),\
             patch.object(self.s,'presign_get_url',return_value='signed'),\
             patch.object(self.s,'original_image_url',return_value='img'):
            r=self.s.run_phase_a(self.a,self.e)
        self.assertEqual(self.a.fidelidade,'ruim')
        self.assertIsNone(self.a.sinais['signals_s3_key'])
        self.assertTrue(r['analyser_status'].startswith('REJECTED_'))

    def test_refine_updates_visible_overlay_and_keeps_history(self):
        with patch.object(self.s,'invoke_lambda',return_value={'status':'CONSISTENCY_PASS','leads':[{'name':'II'}],'artifacts':self.keys}),\
             patch.object(self.s,'presign_get_url',return_value='signed'),\
             patch.object(self.s,'original_image_url',return_value='img'):
            self.s.run_phase_a(self.a,self.e)
        next_art={**self.keys,'result':f'ecg-analyser/{self.a.id}/00000000-0000-4000-8000-000000000000/result.json','signals':f'ecg-analyser/{self.a.id}/00000000-0000-4000-8000-000000000000/signals.npz',
              'trace_masks':f'ecg-analyser/{self.a.id}/00000000-0000-4000-8000-000000000000/trace_masks.npz','working_image':f'ecg-analyser/{self.a.id}/00000000-0000-4000-8000-000000000000/working_image.png','correction_preview':f'ecg-analyser/{self.a.id}/00000000-0000-4000-8000-000000000000/correction_preview.png'}
        response={'status':'MANUAL_REVIEW_REQUIRED','artifacts':next_art,'manual_correction':{'lead':'II'}}
        with patch.object(self.s,'invoke_lambda',return_value=response) as invoke,\
             patch.object(self.s,'presign_get_url',return_value='signed'):
            out=self.s.run_refine(self.a,self.e,'II',[{'lead':'II','mode':'include','points':[{'x':.4,'y':.5}]}])
        self.assertEqual(invoke.call_args.args[1]['operation'],'refine')
        self.assertEqual(self.a.overlay_trace_s3_key,f'ecg-analyser/{self.a.id}/00000000-0000-4000-8000-000000000000/correction_preview.png')
        self.assertEqual(len(self.a.sinais['correction_history']),1)
        self.assertEqual(self.a.sinais['correction_history'][0]['signals'],self.keys['signals'])
        self.assertEqual(out['status'],'MANUAL_REVIEW_REQUIRED')

if __name__=='__main__': unittest.main()


class ServiceSafetyContracts(unittest.TestCase):
    def test_cross_attempt_s3_key_rejected(self):
        service=load_services(); a=MockAttempt(); e=SimpleNamespace(id='35585ae9-4de8-4888-8ac4-23cf14319803')
        fake={'result':'ecg-analyser/other/initial/result.json','signals':'ecg-analyser/other/initial/signals.npz',
              'trace_masks':'ecg-analyser/other/initial/trace_masks.npz',
              'working_image':'ecg-analyser/other/initial/working_image.png',
              'visao_geral':'ecg-analyser/other/initial/visao_geral.png'}
        with patch.object(service,'invoke_lambda',return_value={'status':'CONSISTENCY_PASS','artifacts':fake}):
            with self.assertRaisesRegex(service.UpstreamError,'fora da tentativa'):
                service.run_phase_a(a,e)

class ExtraArtifactProtections(unittest.TestCase):
    def test_initial_output_wrong_filename_rejected(self):
        services=load_services(); attempt=MockAttempt()
        exam=SimpleNamespace(id='35585ae9-4de8-4888-8ac4-23cf14319803')
        prefix='ecg-analyser/'+attempt.id+'/initial/'
        output={'status':'CONSISTENCY_PASS','artifacts': {
            'result': prefix+'working_image.png',
            'signals':prefix+'signals.npz','trace_masks':prefix+'trace_masks.npz',
            'working_image':prefix+'working_image.png',
            'visao_geral':prefix+'visao_geral.png'}}
        with patch.object(services,'invoke_lambda',return_value=output):
            with self.assertRaisesRegex(services.UpstreamError, 'inválidos'):
                services.run_phase_a(attempt,exam)
