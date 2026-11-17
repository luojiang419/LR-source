"""XMP 已有字段与目录确认的合并规则；不执行文件写入或编辑权抢占。"""
import hashlib
import json
import uuid

TAGS={
    'SKU':'sku','BaseSKU':'base_sku','ParentSKU':'parent_sku','ShootVersion':'shoot_version',
    'PhotoRole':'role','ShootDate':'shoot_date','ShootType':'shoot_type','SegmentId':'group_id',
    'AssignmentState':'assignment_state','DecisionId':'decision_id','DecisionSourceId':'decision_source_id',
    'CatalogScopeId':'catalog_scope','Customer':'customer','Brand':'brand','GroupRevision':'group_revision',
    'GroupProductCount':'expected_products','GroupLabelCount':'expected_labels','GroupFileCount':'expected_files',
    'GroupMembersDigest':'members_digest','GroupFilesDigest':'files_digest','GroupManifestScopeId':'group_manifest_scope',
}
COUNTS={'expected_products','expected_labels','expected_files'}
IDENTITIES={'FileId':'file_id','AssetId':'asset_id'}
BUSINESS_KEYS=set(TAGS.values())-{'decision_id','decision_source_id'} | {'rating','boundary'}
READER_VERSION=2


def canonical_id(value):
    return uuid.UUID(str(value)).hex


def boundary(record):
    result=dict(record)
    if result.get('media_type','image')=='image':
        result['boundary']=result.get('rating',0)==2
        result['role']='sku_label' if result['boundary'] else 'product'
    else:result['boundary']=False
    return result


def parse_metadata(full):
    patch={};identities={};errors=[]
    for tag,key in TAGS.items():
        name='XMP-fk:'+tag
        if name not in full:continue
        value=full[name]
        if key in COUNTS:
            if value in (None,''):continue
            try:
                value=int(value)
                if value<0:raise ValueError()
            except (TypeError,ValueError):errors.append('数量字段无效：'+tag);continue
        else:
            value=str(value if value is not None else '')
            if len(value)>(80 if key in ('sku','base_sku','parent_sku') else 500) or any(ord(c)<32 for c in value):
                errors.append('文字字段无效：'+tag);continue
            if value and key in ('group_id','decision_source_id','group_manifest_scope','catalog_scope'):
                try:value=canonical_id(value)
                except (ValueError,TypeError):pass
        patch[key]=value
    for tag,key in IDENTITIES.items():
        if not full.get('XMP-fk:'+tag):continue
        try:identities[key]=canonical_id(full['XMP-fk:'+tag])
        except (ValueError,TypeError):errors.append('文件身份无效：'+tag)
    if 'XMP-xmp:Rating' in full:
        try:
            rating=int(full['XMP-xmp:Rating'])
            if rating not in range(6):raise ValueError()
            patch['rating']=rating
        except (ValueError,TypeError):errors.append('星级无效')
    if 'XMP-fk:DecisionParents' in full:
        try:
            parents=json.loads(full['XMP-fk:DecisionParents'] or '[]')
            if not isinstance(parents,list) or len(parents)>4096 or any(not isinstance(p,str) or not p or len(p)>128 for p in parents):raise ValueError()
            patch['parents']=list(dict.fromkeys(parents))
        except (ValueError,TypeError):errors.append('父修订记录无效')
    try:version=int(full.get('XMP-fk:MetadataSchemaVersion') or 0)
    except (TypeError,ValueError):version=-1;errors.append('元数据格式版本无效')
    if version>1:errors.append('元数据格式高于当前支持版本，请升级后编辑')
    if not patch.get('sku') and patch.get('assignment_state')!='cleared':
        subjects=full.get('XMP-dc:Subject') or []
        if isinstance(subjects,str):subjects=[subjects]
        if not isinstance(subjects,list):subjects=[]
        codes=list(dict.fromkeys(v[4:] for v in subjects if isinstance(v,str) and v.startswith('SKU:') and v[4:].strip()))
        if len(codes)==1 and len(codes[0])<=80 and not any(ord(c)<32 for c in codes[0]):patch['sku']=codes[0]
    complete=version==1 and len(identities)==2 and all(patch.get(k) for k in ('decision_id','decision_source_id','catalog_scope','group_id','group_revision','members_digest','files_digest')) and all(k in patch for k in (*COUNTS,'rating')) and bool(patch.get('sku'))
    kind='complete' if complete else 'sku' if patch.get('sku') else 'rating' if 'rating' in patch else 'empty'
    return {'patch':patch,'identities':identities,'schema':version,'kind':kind,'errors':errors}


def merge_fields(current,patch):
    result=dict(current)
    clearing=patch.get('assignment_state')=='cleared'
    for key,value in patch.items():
        if key in ('sku','base_sku','catalog_scope','decision_source_id') and value=='' and not clearing:continue
        if key=='assignment_state' and current.get('sku') and not patch.get('sku') and not clearing:continue
        result[key]=value
    if clearing:result['sku']=''
    if result.get('sku'):
        result['assignment_state']='confirmed'
        if not result.get('base_sku'):result['base_sku']=result['sku']
    return boundary(result)


def reconcile(current,metadata,*,dataset_id=''):
    patch=metadata['patch'];incoming=patch.get('decision_id') or '';known=current.get('decision_id') or ''
    if metadata['errors']:return {'relation':'conflict','reason':'；'.join(metadata['errors']),'fields':[],'can_use_file':False}
    for key,value in metadata['identities'].items():
        expected=current.get(key) or (current.get('id') if key=='file_id' else None)
        if expected and expected!=value:return {'relation':'identity','reason':'文件永久身份与目录记录不一致','fields':[key],'can_use_file':False}
    candidate=merge_fields(current,patch)
    differences=[key for key in sorted(BUSINESS_KEYS) if key in patch and current.get(key) not in (None,'') and candidate.get(key)!=boundary(current).get(key)
                 and not (key=='assignment_state' and current.get(key)=='unassigned' and not current.get('sku') and patch.get('sku'))]
    if current.get('assignment_state')=='cleared' and candidate.get('sku') and 'sku' not in differences:differences.append('sku')
    if not known:return {'relation':'bootstrap','record':candidate}
    if incoming and incoming in current.get('parents',[]):return {'relation':'archive_newer','record':boundary(current)}
    scope_change=bool(patch.get('catalog_scope') and current.get('catalog_scope') and patch['catalog_scope']!=current['catalog_scope'])
    if scope_change:return {'relation':'conflict','reason':'客户／品牌作用域不同，需在正确数据集中处理','fields':['catalog_scope'],'can_use_file':False}
    if incoming==known:
        if differences:return {'relation':'conflict','reason':'同一业务修订的文件字段与目录确认不一致','fields':differences,'can_use_file':True}
        return {'relation':'same','record':candidate}
    if not incoming:
        if current.get('write_state')=='pending':return {'relation':'archive_newer','record':boundary(current)}
        if differences:return {'relation':'conflict','reason':'XMP没有可比较修订，已保留目录确认，请选择采用来源','fields':differences,'can_use_file':True}
        # 无修订的评级侧车只能补缺，不把父链和已确认身份清空。
        return {'relation':'same','record':candidate}
    allowed_sources={v for v in (dataset_id,current.get('decision_source_id')) if v}
    if known in patch.get('parents',[]) and not scope_change and patch.get('decision_source_id') in allowed_sources:
        return {'relation':'file_newer','record':candidate}
    return {'relation':'conflict','reason':'客户／品牌作用域不同，需在正确数据集中处理' if scope_change else 'XMP与目录确认的修订分叉或来源无法证明','fields':differences,'can_use_file':not scope_change}


def conflict_data(current,metadata,outcome):
    info={k:outcome[k] for k in ('reason','fields','can_use_file')}
    info['can_keep_archive']=not metadata['errors']
    info.update(file=metadata,archive={k:current[k] for k in ('decision_id','sku','rating','group_id','customer','brand') if k in current})
    proof={'decision':current.get('decision_id'),'signature':current.get('signature'),'sidecar':current.get('sidecar_signature'),'file':metadata}
    info['token']=hashlib.sha256(json.dumps(proof,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return info
