import time,importlib,json,base64
from pathlib import Path
from app.config import MODULES, SAMPLES
from app.errors import DomainError
from app.manifest import identity,manifest
from app.scenarios import default_parameters

def load(sample):
    if sample not in SAMPLES:raise DomainError('SAMPLE','样例名称无效')
    return (Path(__file__).resolve().parents[1]/'sample_data'/sample).read_bytes()

def parameters_for(sample,parameters):
    merged=default_parameters(sample);merged.update(parameters or {});return merged

def run(content,parameters=None,stage=7,sample=None):
    if len(content)>2_000_000:raise DomainError('SIZE','文件超过2MB',413)
    params=parameters or {}
    if not isinstance(params,dict):raise DomainError('PARAMETERS','参数必须是对象')
    if sample:params=parameters_for(sample,params)
    start=time.monotonic();state=None
    for n,entry in enumerate(MODULES,1):
        if n>stage:break
        fn=importlib.import_module('app.modules.'+entry[0]+'.service').execute
        state=fn(content,params) if n==1 else fn(state)
    # Decimal values are serialized as strings before any storage or response.
    state=json.loads(json.dumps(state,default=str,ensure_ascii=False))
    state['run_id']=identity(content,params);state['manifest']=manifest(content,params,time.monotonic()-start)
    state['source_b64']=base64.b64encode(content).decode('ascii')
    state['sample']=sample or state.get('sample','')
    if stage>=7:
        from app.modules.m7_archive.export_out import checksum
        state['archive']['sha256']=checksum(state)
    return state
