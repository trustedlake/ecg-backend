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
        AWS_LAMBDA_ECG_ANALYSER='new-lambda', AWS_LAMBDA_ECG_READING='read-lambda',
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

    def test_forward_calibration_and_store_chosen_method(self):
        m=lambda n,sc:{'method':n,'status':'OK','selector_score':sc,'files':{'leads':{'bucket':'r','key':'k/'+n+'/leads.json'},'image':{'bucket':'r','key':'k/'+n+'/p.png'},'json':{'bucket':'r','key':'k/'+n+'/result.json'}}}
        out={'operation':'read','status':'OK','warnings':[],'result':{'methods':[m('a',10),m('b',80)]}}
        self.e.ecg_layout='3x4+1'
        with patch.object(self.s,'invoke_lambda',return_value=out) as invoke,             patch.object(self.s,'_read_json',return_value={'leads':[{'name':'II'}]}),             patch.object(self.s,'presign_get_url',return_value='signed'),             patch.object(self.s,'original_image_url',return_value='img'):
            result=self.s.run_phase_a(self.a,self.e)
        self.assertEqual(invoke.call_args.args[0],'read-lambda')
        self.assertEqual(invoke.call_args.args[1]['calibration'],{'speed_mm_s':50,'gain_mm_mv':20})
        self.assertEqual(self.a.sinais['chosen_method'],'b')
        self.assertEqual(self.a.overlay_trace_s3_key,'k/b/p.png')
        self.assertEqual(result['analyser_status'],'OK')

    def test_no_calibration_no_lambda(self):
        self.a.calibracao=None
        with patch.object(self.s,'invoke_lambda') as invoke:
            with self.assertRaisesRegex(self.s.UpstreamError,'Calibração'):
                self.s.run_phase_a(self.a,self.e)
            invoke.assert_not_called()

    def test_refused_without_signal(self):
        out={'operation':'read','status':'REFUSED','warnings':[],'error':{'code':'READ_REFUSED_LOW_CONTRAST'},'result':None}
        self.e.ecg_layout='3x4+1'
        with patch.object(self.s,'invoke_lambda',return_value=out),             patch.object(self.s,'presign_get_url',return_value='signed'),             patch.object(self.s,'original_image_url',return_value='img'):
            r=self.s.run_phase_a(self.a,self.e)
        self.assertEqual(self.a.fidelidade,'ruim')
        self.assertEqual(self.a.sinais['leads'],[])
        self.assertTrue(r['analyser_status'].startswith('REJECTED_'))

    def test_error_envelope_raises(self):
        out={'operation':'read','status':'ERROR','warnings':[],'error':{'code':'READ_SELECTOR_UNAVAILABLE','message':'m'}}
        self.e.ecg_layout='3x4+1'
        with patch.object(self.s,'invoke_lambda',return_value=out):
            with self.assertRaisesRegex(self.s.UpstreamError,'READ_SELECTOR_UNAVAILABLE'):
                self.s.run_phase_a(self.a,self.e)

    def test_refine_updates_visible_overlay_and_keeps_history(self):
        self.a.sinais={'result_s3_key':self.keys['result'],'signals_s3_key':self.keys['signals'],'working_image_s3_key':self.keys['working_image']}
        self.a.mascara_s3_key=self.keys['trace_masks'];self.a.overlay_trace_s3_key=self.keys['visao_geral']
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

