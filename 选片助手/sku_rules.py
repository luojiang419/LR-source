"""公司款号规则：确定前后缀自动应用，模糊包含仅推荐。"""
import json
import re
import unicodedata


DEFAULT_RULES=[{'kind':'prefix','value':'LC'}]
DEFAULT_LENGTH_FILTER={'min_exclusive':None,'max_exclusive':None}


def validate_length_filter(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULT_LENGTH_FILTER):raise ValueError('无效的推荐款号长度规则')
    result={k:value.get(k) for k in DEFAULT_LENGTH_FILTER}
    for key,limit in result.items():
        if limit is not None and (type(limit) is not int or not 0<=limit<=81):raise ValueError('字符数须为 0–81 的整数，留空表示不限')
    low=result['min_exclusive'];high=result['max_exclusive']
    if (0 if low is None else low)+1 >= (81 if high is None else high):raise ValueError('长度范围内至少需要包含一个 1–80 的整数')
    return result


def length_allowed(code,limits):
    size=len(unicodedata.normalize('NFKC',code).strip())
    return 1<=size<=80 and (limits['min_exclusive'] is None or size>limits['min_exclusive']) and (limits['max_exclusive'] is None or size<limits['max_exclusive'])


def validate_rules(rules):
    if not isinstance(rules,list) or len(rules)>50:raise ValueError('最多设置 50 条款号规则')
    result=[]
    for rule in rules:
        if not isinstance(rule,dict) or rule.get('kind') not in ('prefix','suffix','contains'):raise ValueError('无效的款号规则类型')
        value=rule.get('value')
        if not isinstance(value,str):raise ValueError('请填写匹配文字')
        value=unicodedata.normalize('NFKC',value).strip().upper()
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9._/-]{0,39}',value):raise ValueError('匹配文字需为 1–40 位字母、数字或 . _ / -，以字母数字开头')
        item={'kind':rule['kind'],'value':value}
        if item not in result:result.append(item)
    return result


def reading_tokens(readings):
    tokens=[]
    for reading in readings:
        sources=[reading.get('text',''),*(reading.get('codes') or []),*(reading.get('ocr_candidates') or [])]
        for source in sources:
            for token in re.findall(r'[A-Z0-9]+(?:[._/-][A-Z0-9]+)*',unicodedata.normalize('NFKC',str(source)).upper()):
                if len(token)<=80 and re.search(r'\d',token) and token not in tokens:tokens.append(token)
    return tokens


def match_rules(readings,rules):
    tokens=reading_tokens(readings);exact=[];suggested=[]
    for token in tokens:
        for rule in rules:
            value=rule['value'];kind=rule['kind']
            hit=token.startswith(value) if kind=='prefix' else token.endswith(value) if kind=='suffix' else value in token
            if hit and len(token)>len(value):
                target=suggested if kind=='contains' else exact
                if token not in target:target.append(token)
    return {'exact':exact,'suggested':list(dict.fromkeys(exact+suggested))}


class SkuRules:
    def sku_rules(self):
        with self.connect() as c:
            row=c.execute("SELECT value FROM settings WHERE key='sku_match_rules'").fetchone()
            length=c.execute("SELECT value FROM settings WHERE key='sku_recommendation_length'").fetchone()
        return {'rules':json.loads(row[0]) if row else [dict(r) for r in DEFAULT_RULES],
                'length_filter':json.loads(length[0]) if length else dict(DEFAULT_LENGTH_FILTER)}

    def set_sku_rules(self,rules,length_filter=None):
        rules=validate_rules(rules)
        if length_filter is not None:length_filter=validate_length_filter(length_filter)
        with self.lock,self.connect() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES ('sku_match_rules',?)",(json.dumps(rules),))
            if length_filter is not None:c.execute("INSERT OR REPLACE INTO settings VALUES ('sku_recommendation_length',?)",(json.dumps(length_filter),))
        return self.sku_rules()

    def recommended_codes(self,readings):
        config=self.sku_rules();limits=config['length_filter']
        values=match_rules(readings,config['rules'])['suggested']+[code for reading in readings for code in reading.get('codes') or []]
        # 设置长度筛选后，原文中符合范围的含数字编码也可进入推荐，避免只依赖模型的主款号字段。
        if any(value is not None for value in limits.values()):values+=reading_tokens(readings)
        return list(dict.fromkeys(code.strip() for code in values if isinstance(code,str) and length_allowed(code,limits) and not any(ord(ch)<32 for ch in code)))[:12]

    def apply_sku_rules(self,sid,cid,readings):
        with self.lock:
            config=self.sku_rules();matches=match_rules(readings,config['rules'])
            if len(matches['exact'])!=1 or not self.archive:return None
            if not length_allowed(matches['exact'][0],config['length_filter']):return None
            details=self.ocr_details(sid,cid)
            if details['selected_sku'] or details['target_capture_id']!=cid:return None
            with self.archive.connect() as c:
                # 人工归属优先；自动规则不能覆盖先前编辑。
                assigned=c.execute('SELECT a.sku FROM assets a JOIN catalog_links l ON l.asset=a.id WHERE l.session=? AND l.capture=?',(sid,cid)).fetchone()
            if assigned and assigned['sku']:return None
            return self.choose_ocr_sku(sid,cid,matches['exact'][0],details['reading_key'],details['image_key'],automatic=True)
