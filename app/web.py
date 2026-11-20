import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load,parameters_for
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    parameters=data.get('parameters',{})
    if not isinstance(parameters,dict):raise DomainError('PARAMETERS','参数必须是对象')
    if isinstance(content,str):
        # Inline CSV cannot be tied to a built-in sample's composition sheet.
        return content.encode(),parameters,None
    return load(sample),parameters_for(sample,parameters),sample


def api(stage):
    content,parameters,sample=payload();return jsonify(run(content,parameters,stage,sample))


def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    if 'sample' in request.args:
        source=load(request.args['sample']);sample=request.args['sample']
    else:
        source=base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
        sample=state.get('sample') or None
    data=run(source,state.get('manifest',{}).get('parameters',{}),stage,sample)
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest)
