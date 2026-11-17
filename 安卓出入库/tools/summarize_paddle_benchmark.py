"""汇总真机模型结果；区分候选正确、自动错存、人工复核和手写字符误差。"""
from pathlib import Path
import argparse
import json
import statistics
import unicodedata

def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).upper() if c.isalnum())

def distance(left,right):
    row=list(range(len(right)+1))
    for i,a in enumerate(left,1):
        current=[i]
        for j,b in enumerate(right,1):current.append(min(current[-1]+1,row[j]+1,row[j-1]+(a!=b)))
        row=current
    return row[-1]

def summarize(report):
    cases=report['results'];sku=[c for c in cases if 'expected' in c['sample']];hand=[c for c in cases if 'expectedText' in c['sample']]
    correct=wrong=accepted=0;details=[]
    for case in sku:
        expected=case['sample']['expected'];preferred=case.get('preferred',[]);top=preferred[0] if preferred else ''
        exact=top==expected;automatic=case.get('automatic',False);saved=case.get('sku','')
        correct+=exact;accepted+=automatic and saved==expected;wrong+=automatic and saved!=expected
        details.append({'file':case['sample']['file'],'kind':case['sample']['kind'],'expected':expected,'candidate':top,'exact':exact,'automatic':automatic,'saved':saved,'ms':case['elapsedMs']})
    errors=[]
    for case in hand:
        expected=normalized(case['sample']['expectedText']);predicted=normalized(case.get('text',''));errors.append(distance(expected,predicted)/max(1,len(expected)))
    return {'model':report['model'],'device':report['device'],'skuCases':len(sku),'exactTop':correct,'autoCorrect':accepted,'wrongAuto':wrong,'handwritingCER':statistics.mean(errors) if errors else None,'medianMs':statistics.median([c['elapsedMs'] for c in cases]),'errors':sum('error' in c for c in cases),'maxLoadedPssMb':max((c.get('timing',{}).get('loadedPssKb',0)/1024 for c in cases),default=0),'details':details}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);args=parser.parse_args()
    reports=[summarize(json.loads(p.read_text(encoding='utf-8'))) for p in args.directory.glob('result-*.json')]
    reports.sort(key=lambda r:(r['errors'],-r['exactTop'],r['wrongAuto'],r['handwritingCER'] if r['handwritingCER'] is not None else 10,r['medianMs']))
    (args.directory/'summary.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    for r in reports:print(json.dumps({k:v for k,v in r.items() if k!='details'},ensure_ascii=False))
