import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get("reconcile",{}),ensure_ascii=False,sort_keys=True,default=str)])
    summary=state.get('scope_summary',{})
    if summary.get('applicable'):
        writer.writerow(['范围裁决',rid,'','','',json.dumps(summary,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state.get('parcels',[]):writer.writerow(['地块',rid,'','','',json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    # Every original report, including superseded ones, is archived so the
    # composite sample and its retests never disappear from the record.
    for row in state.get('records',[]):writer.writerow(['报告',rid,row.get('line',''),row.get('id',''),row.get('disposition',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
