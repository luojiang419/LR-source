"""只接续未分配的临时身份；已有业务确认与不同快门身份继续走冲突流程。"""
from datetime import datetime
from pathlib import PurePosixPath
import uuid

from capture_metadata_reconcile import parse_metadata
from portable_capture_store import digest,encoded
from sku_folder_archive import camera_metadata,capture_key


def recover(old,full,records,groups):
    if not old or old.get('sku') or old.get('group_id') or old.get('decision_id') or old.get('assignment_state') not in (None,'','unassigned') or old.get('metadata_conflict'):
        return None
    incoming=parse_metadata(full)
    if incoming['errors'] or incoming['patch'].get('decision_id') or incoming['patch'].get('assignment_state')=='cleared':return None
    if incoming['patch'].get('rating')==2:return None
    try:
        fid=uuid.UUID(str(full['XMP-fk:FileId'])).hex;asset=uuid.UUID(str(full['XMP-fk:AssetId'])).hex
        gid=uuid.UUID(str(full['XMP-fk:SegmentId'])).hex
        camera=camera_metadata(full);capture=capture_key(camera)
        if not old.get('capture_identity') or old['capture_identity']!=capture:return None
        raw=camera['combined']
        captured=datetime.fromisoformat((raw[:10].replace(':','-')+raw[10:]).replace(' ','T',1))
    except (KeyError,TypeError,ValueError):return None
    group=next((g for g in groups if g['id']==gid and g.get('status')=='confirmed' and g.get('sku')),None)
    if group is None:return None
    labels=[];parent=PurePosixPath(old['relative_path']).parent
    for row in records:
        if not row.get('boundary') or row.get('rating')!=2 or not row.get('sku') or PurePosixPath(row['relative_path']).parent!=parent:continue
        try:
            other=row.get('camera',{})
            if (other.get('model','').casefold(),str(other.get('serial','')).lstrip('0'))!=(camera['model'].casefold(),camera['serial'].lstrip('0')):continue
            stamp=datetime.fromisoformat(row['time'])
            if stamp.date()!=captured.date() or stamp.tzinfo!=captured.tzinfo:continue
        except (ValueError,TypeError,KeyError):continue
        if old.get('catalog_scope') and row.get('catalog_scope') and old['catalog_scope']!=row['catalog_scope']:continue
        labels.append((stamp,row))
    endings=sorted(((stamp,row) for stamp,row in labels if stamp>=captured),key=lambda entry:entry[0])
    if not endings:return None
    end,label=endings[0]
    if sum(stamp==end for stamp,_ in endings)!=1 or label['asset_id']!=group.get('label') or label.get('sku')!=group['sku']:return None
    if any(stamp==captured and row['asset_id']!=asset for stamp,row in labels):return None
    kind=label.get('shoot_type')
    if not kind or kind=='未分类' or incoming['patch'].get('shoot_type')!=kind:return None
    for key in ('catalog_scope','customer','brand'):
        if incoming['patch'].get(key) and incoming['patch'][key]!=label.get(key):return None
    if any(r['id']!=old['id'] and (r.get('file_id')==fid or r.get('asset_id')==asset) for r in records):return None
    token=group.get('decision_id') or label.get('group_revision') or label.get('decision_id') or uuid.uuid5(uuid.UUID(gid),'bootstrap-recovery:'+group['sku']).hex
    value={**old,'file_id':fid,'asset_id':asset,'sku':group['sku'],'group_id':gid,'shoot_type':kind,
           'assignment_state':'confirmed','assignment_source':'verified_interval_recovery','decision_id':token,'group_revision':token,
           'write_state':'pending','write_policy':'full','parents':list(dict.fromkeys([*old.get('parents',[]),*label.get('parents',[])])),
           'identity_recovery':{'old_file_id':old.get('file_id'),'old_asset_id':old.get('asset_id'),'capture_identity':capture,'group_id':gid}}
    for key in ('base_sku','parent_sku','shoot_version','customer','brand','catalog_scope','shoot_date','decision_source_id'):
        if label.get(key) is not None:value[key]=label[key]
    if 'rating' in incoming['patch']:value['rating']=incoming['patch']['rating']
    value.pop('identity_conflict',None)
    for key in list(value):
        if key.startswith('inherited_'):value.pop(key)
    members=list(dict.fromkeys([*group.get('members',[]),asset]))
    physical=[value if r['id']==old['id'] else r for r in records if r.get('group_id')==gid or r['id']==old['id']]
    updated={**group,'members':members,'decision_id':token,'expected_products':len({r['asset_id'] for r in physical if not r.get('boundary')}),
             'expected_labels':len({r['asset_id'] for r in physical if r.get('boundary')}),'expected_files':len(physical),
             'members_digest':digest(encoded(sorted(members))),'files_digest':digest(encoded(sorted(r.get('file_id') or r['id'] for r in physical)))}
    for key in ('expected_products','expected_labels','expected_files','members_digest','files_digest'):value[key]=updated[key]
    return value,updated
