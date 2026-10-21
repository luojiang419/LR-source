"""可解释款号匹配：保留真实字符，易混字符仅用于候选距离。"""
import re
import math
import unicodedata

CONFUSIONS=[set('0OQ'),set('1IL'),set('2Z'),set('5S'),set('8B'),set('6G')]


def normalize(value):
    return re.sub(r'[^A-Z0-9]','',unicodedata.normalize('NFKC',str(value)).upper())


def extract_codes(text):
    result=[]
    for token in re.findall(r'[A-Za-z0-9Ａ-Ｚａ-ｚ０-９][A-Za-z0-9Ａ-Ｚａ-ｚ０-９\-－—_ /.]{3,45}',str(text)):
        token=token.strip();key=normalize(token)
        if len(key)>=4 and re.search(r'\d',key) and key not in [normalize(x) for x in result]:result.append(token)
    return result[:12]


def distance(a,b):
    d=[[0.]*(len(b)+1) for _ in range(len(a)+1)]
    for i in range(len(a)+1):d[i][0]=float(i)
    for j in range(len(b)+1):d[0][j]=float(j)
    for i,x in enumerate(a,1):
        for j,y in enumerate(b,1):
            cost=0 if x==y else (.2 if any(x in g and y in g for g in CONFUSIONS) else 1.)
            d[i][j]=min(d[i-1][j]+1,d[i][j-1]+1,d[i-1][j-1]+cost)
            if i>1 and j>1 and a[i-2]==y and x==b[j-2]:d[i][j]=min(d[i][j],d[i-2][j-2]+.7)
    return d[-1][-1]


def match(readings,rows):
    groups={}
    for row in rows:groups.setdefault(row['normalized'],[]).append(row)
    ranked=[];evidence=[];ambiguous=False
    for reading in readings:
        codes=reading.get('codes') or extract_codes(reading.get('text',''))
        codes=list(dict.fromkeys(normalize(c) for c in codes if normalize(c)))[:12]
        if len(codes)!=1:
            if len(codes)>1:ambiguous=True
            continue
        code=codes[0];scores=sorted([(distance(code,k),k) for k in groups],key=lambda x:(x[0],x[1]))
        if not scores:continue
        cost,key=scores[0];score=1-cost/max(len(code),len(key),1)
        next_score=1-scores[1][0]/max(len(code),len(scores[1][1]),1) if len(scores)>1 else 0
        confidence=reading.get('confidence',0)
        if type(confidence) not in (int,float) or not math.isfinite(confidence):confidence=0
        evidence.append({'key':key,'cost':round(cost,4),'score':round(score,4),'margin':round(score-next_score,4),
                         'confidence':confidence,'is_label':reading.get('is_label') is True,
                         'engine':reading.get('engine','')})
        for c,k in scores[:5]:
            for row in groups[k]:ranked.append({'row_id':row['id'],'sheet':row['sheet'],'row':row['excel_row'],'sku':row['sku'],'score':round(1-c/max(len(code),len(k),1),4)})
    candidates={}
    for item in ranked:
        previous=candidates.get(item['row_id'])
        if previous is None or previous['score']<item['score']:candidates[item['row_id']]=item
    candidates=sorted(candidates.values(),key=lambda x:(-x['score'],x['sheet'],x['row']))[:12]
    eligible=[e for e in evidence if e['confidence']>=.9 and e['is_label']]
    exact={e['key'] for e in eligible if e['cost']==0}
    if not ambiguous and len(exact)==1 and all(e['key'] in exact for e in evidence) and len(groups[next(iter(exact))])==1:
        row=groups[next(iter(exact))][0]
        return {'status':'matched','row_ids':[row['id']],'reason':'高置信唯一精确匹配','candidates':candidates,'evidence':evidence}
    good=[e for e in eligible if e['cost']<=.35 and e['score']>=.97 and e['margin']>=.06]
    if not ambiguous and len(good)>=2 and len({e['key'] for e in good})==1 and len({e['engine'] for e in good})>=2:
        key=good[0]['key']
        if len(groups[key])==1 and all(e['key']==key for e in evidence):
            return {'status':'matched','row_ids':[groups[key][0]['id']],'reason':'两路识别一致且单个易混字符匹配','candidates':candidates,'evidence':evidence}
    return {'status':'review','row_ids':[],'reason':'需确认：重复款号、识别不确定或候选过近' if candidates else '未读出可用款号，请重试或手动选择',
            'candidates':candidates,'evidence':evidence}


def match_automatic(readings,rows):
    """用户主动开启的全图模式：不伪造模型置信度，用文档候选距离判定。"""
    result=match(readings,rows);groups={}
    for row in rows:groups.setdefault(row['normalized'],[]).append(row)
    accepted=set();ambiguous=False;evidence=[]
    for reading in readings:
        for value in (reading.get('codes') or extract_codes(reading.get('text','')))[:12]:
            code=normalize(value)
            if len(code)<4:continue
            ranked=sorted((distance(code,key),key) for key in groups)
            if not ranked:continue
            cost,key=ranked[0];score=1-cost/max(len(code),len(key),1)
            second=1-ranked[1][0]/max(len(code),len(ranked[1][1]),1) if len(ranked)>1 else 0
            if cost==0 or cost<=1 and score>=.90:
                if len(groups[key])!=1 or cost!=0 and score-second<.06:ambiguous=True;continue
                accepted.add(key);evidence.append({'code':code,'sku':groups[key][0]['sku'],'score':round(score,4),'margin':round(score-second,4)})
    if len(accepted)==1 and not ambiguous:
        row=groups[next(iter(accepted))][0]
        return {**result,'status':'matched','row_ids':[row['id']],'reason':'文档唯一精确或高相似度匹配','automatic_evidence':evidence}
    return {**result,'status':'review','row_ids':[],'reason':'文档候选冲突或相似度不足，未自动入库','automatic_evidence':evidence}
