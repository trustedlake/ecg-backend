"""Manual end-to-end smoke using real Django + S3 + single Lambda.

Requires: explicit SMOKE_IMAGE_PATH, production/test DB configured and temporary
user authorization. DO NOT run without a disposable test environment.
"""
import os, pathlib, uuid
import boto3
import requests
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from django.conf import settings

BASE=os.environ.get('SMOKE_BASE_URL','http://127.0.0.1:8001').rstrip('/')

def main():
    image=os.environ.get('SMOKE_IMAGE_PATH')
    if not image or not pathlib.Path(image).is_file():
        raise SystemExit('Set SMOKE_IMAGE_PATH to a controlled ECG photo')
    if os.environ.get('SMOKE_ALLOW_MUTATIONS')!='YES':
        raise SystemExit('SMOKE_ALLOW_MUTATIONS=YES is required; run only in a disposable environment')
    username='smoke-'+uuid.uuid4().hex[:10]
    User=get_user_model()
    user=User.objects.create_user(username=username,email=username+'@example.test',password='Smoke!123',name='Smoke',crm='TEMP')
    session=requests.Session()
    attempt_id=None
    uploaded_image_key=None
    try:
        token=Token.objects.create(user=user)
        session.headers['Authorization']='Token '+token.key
        def api(method,path,**kwargs):
            r=session.request(method,BASE+path,timeout=(15,390),**kwargs)
            print(method,path,'->',r.status_code)
            r.raise_for_status()
            return r.json()
        api('POST','/api/auth/login/',json={'username':username.upper(),'password':'Smoke!123'})
        exam=api('POST','/api/exams/',json={'name':'Paciente Smoke','age':44,'gender':'M','reason':'teste'})
        eid=exam['id']
        attempt=api('POST',f'/api/exams/{eid}/attempts/',json={'filename':pathlib.Path(image).name,'content_type':'image/png','speed_mm_s':25,'gain_mm_mv':10})
        aid=attempt['attempt_id']
        attempt_id=aid
        uploaded_image_key=attempt['image_s3_key']
        b=pathlib.Path(image).read_bytes()
        r=requests.put(attempt['upload_url'],data=b,headers={'Content-Type':'image/png'},timeout=90)
        r.raise_for_status()
        result=api('POST',f'/api/exams/{eid}/attempts/{aid}/process/')
        assert result['analyser_status'] in ('CONSISTENCY_PASS','REVIEW_REQUIRED','REJECTED_LOW_TRACE_GRID_CONTRAST')
        state=api('GET',f'/api/exams/{eid}/attempts/{aid}/process/')
        assert state['analyser_status']==result['analyser_status']
        if result['analyser_status'].startswith('REJECTED_'):
            assert session.post(BASE+f'/api/exams/{eid}/attempts/{aid}/aprovar/').status_code==409
        else:
            # Refine requires user-supplied valid lead-specific pixel markings;
            # do not invent a stroke that may fall outside the lead ROI.
            if os.environ.get('SMOKE_REFINE_JSON'):
                import json
                paint=json.loads(pathlib.Path(os.environ['SMOKE_REFINE_JSON']).read_text())
                api('POST',f'/api/exams/{eid}/attempts/{aid}/refine/',json=paint)
            action=os.environ.get('SMOKE_ACTION','approve')
            if action == 'reject':
                api('POST',f'/api/exams/{eid}/attempts/{aid}/rejeitar/')
            elif action == 'approve':
                api('POST',f'/api/exams/{eid}/attempts/{aid}/aprovar/')
            else:
                raise ValueError('SMOKE_ACTION must be approve or reject')
        detail=api('GET',f'/api/exams/{eid}/')
        assert detail['id']==eid
        print('OK: single Lambda and persisted state; S3 cleanup follows')
    finally:
        # Restrict cleanup strictly to the UUID allocated for this smoke run.
        try:
            s3=boto3.Session(region_name=settings.AWS_REGION).client('s3')
            if uploaded_image_key:
                s3.delete_object(Bucket=settings.AWS_IMAGES_BUCKET,Key=uploaded_image_key)
            if attempt_id:
                prefix='ecg-analyser/'+str(attempt_id)+'/'
                paginator=s3.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=settings.AWS_RESULTS_BUCKET,Prefix=prefix):
                    objs=[{'Key':x['Key']} for x in page.get('Contents',[])]
                    if objs:s3.delete_objects(Bucket=settings.AWS_RESULTS_BUCKET,Delete={'Objects':objs,'Quiet':True})
        except Exception as exc:
            print('WARNING: S3 cleanup failed; cleanup manually:',exc)
        Token.objects.filter(user=user).delete()
        user.delete()

if __name__=='__main__': main()
