"""只在新建根目录测试副本内运行 SKU 归档，保留真实照片与独立校验结果。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '选片助手'))
from PIL import Image, ImageOps
from metadata_writer import MetadataWriter, sha, raw_integrity
from sku_folder_archive import SkuFolderArchive, camera_metadata, capture_key, image_pixels


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def verify(source, output, resume=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if (output.exists() and not resume) or output.parent != ROOT:
        raise ValueError('测试输出必须是项目根目录下全新且不存在的文件夹')
    output.mkdir(exist_ok=resume)
    small, final, target = [output / name for name in ('小档', '大档', 'sku库')]
    for p in (small, final, target):
        p.mkdir(exist_ok=resume)
    data = small / '.验证资料'
    data.mkdir(exist_ok=resume)
    writer = MetadataWriter(ROOT, data)
    baseline, fixtures, checks = {}, [], []
    database = small / '现场SKU.sqlite'
    database_records = []
    try:
        if resume:
            baseline=json.loads((output/'原件只读清单.json').read_text(encoding='utf8'))
            fixtures=json.loads((output/'样片与测试款号对照.json').read_text(encoding='utf8'))
            marker=target/'LC78'/'既有母SKU目录保留.txt'
        else:
            with sqlite3.connect(database) as c:
                c.execute('CREATE TABLE captures(id TEXT PRIMARY KEY,source TEXT NOT NULL,fingerprint TEXT NOT NULL,sku TEXT NOT NULL,scope TEXT NOT NULL,rating INTEGER NOT NULL,metadata TEXT NOT NULL)')
            assignments = ['LC7877847']*2 + ['LC7877852']*3 + ['LC7979103']*3
            # SKU 为本次测试明确指定的备案值；不声称是样片原有业务款号。
            for i in range(8):
                stem = f'XSY_{5433+i}'
                original_jpg, original_raw = source/(stem+'.JPG'), source/(stem+'.CR3')
                if not original_jpg.is_file() or not original_raw.is_file():
                    raise ValueError('真实样片配对缺失：'+stem)
                for p in (original_jpg, original_raw):
                    baseline[str(p)] = sha(p)
                directory = small / ('2026-10-20/上午/机位A' if i < 3 else '2026-10-20/下午/机位B')
                directory.mkdir(parents=True, exist_ok=True)
                onsite = directory / f'现场小档-{i+1:02}.JPG'
                with Image.open(original_jpg) as im:
                    im.thumbnail((1200,1200))
                    im.convert('RGB').save(onsite,'JPEG',quality=85)
                # 只向小档副本迁移相机信息，包含 Canon MakerNotes，不伪造拍摄 EXIF。
                writer.run('-overwrite_original','-TagsFromFile',original_jpg,'-all:all',onsite)
                original_key = capture_key(camera_metadata(writer.read(original_jpg,full=True)))
                info = camera_metadata(writer.read(onsite,full=True))
                assert capture_key(info) == original_key
                record = {'source':str(onsite),'sku':assignments[i],'scope':'细节图','rating':(i%5)+1}
                from sku_folder_archive import metadata_fields
                fields = metadata_fields(record, original_key)
                content, metadata_path, _ = writer.write(onsite,fields,sha(onsite))
                assert metadata_path == onsite
                onsite.write_bytes(content)
                assert capture_key(camera_metadata(writer.read(onsite,full=True))) == original_key
                assert writer.read(onsite)['XMP-fk:SKU'] == assignments[i]
                with sqlite3.connect(database) as c:
                    c.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?)',
                              (str(i+1),str(onsite),original_key,assignments[i],'细节图',record['rating'],json.dumps(info)))
                row = {'id':i+1,'onsite':str(onsite),'original_jpg':str(original_jpg),'original_raw':str(original_raw),
                       'fingerprint':original_key,'sku':assignments[i],'deleted_after_selection':i>=6,'final':[]}
                # 前两张 RAW+JPG，接着两张只留 RAW，最后两张仅 JPG；末两张快门废片不进入最终目录。
                if i < 6:
                    formats = ['CR3','JPG'] if i<2 else ['CR3'] if i<4 else ['JPG']
                    for extension in formats:
                        final_dir = final / ('第一批/保留' if i<3 else '第二批/保留')
                        final_dir.mkdir(parents=True,exist_ok=True)
                        renamed = final_dir / f'后期保留-{i+1:02}-{extension.lower()}.{extension}'
                        original = original_raw if extension=='CR3' else original_jpg
                        shutil.copy2(original,renamed)
                        assert sha(renamed) == baseline[str(original)]
                        assert capture_key(camera_metadata(writer.read(renamed,full=True))) == original_key
                        row['final'].append(str(renamed))
                fixtures.append(row)
                print(f'PREPARED {i+1}/8 {stem} -> {assignments[i]}',flush=True)
            save(output/'原件只读清单.json',baseline)
            save(output/'样片与测试款号对照.json',fixtures)
            assert len(set(r['fingerprint'] for r in fixtures)) == 8
            (target/'LC78').mkdir()
            marker = target/'LC78'/'既有母SKU目录保留.txt'
            marker.write_text('测试前已存在的母SKU目录，归档应复用并保留该文件。',encoding='utf8')
            # 已有调色和自定义 XML 节点的来源侧车，用于验证合并不丢用户参数。
            side = Path(fixtures[0]['final'][0]).with_suffix('.xmp')
            side.write_text('''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:Exposure2012="+0.35" crs:Temperature="5200"/></rdf:RDF></x:xmpmeta>''',encoding='utf8')
        final_before = {str(p):sha(p) for p in final.rglob('*') if p.is_file()}
        small_before = {str(p):sha(p) for p in small.rglob('*.JPG')}
        archive = SkuFolderArchive(writer,database)
        plan = archive.plan(small,final,target,preprocess=False,include_label_images=False)
        assert plan['expected']==8 and plan['expected_xmp']==4 and plan['deleted_reference_count']==2
        assert all(Path(r['source']).name != Path(r['reference']).name for r in plan['items'])
        save(output/'归档执行计划.json',plan)
        report = archive.execute(plan)
        save(target/'归档结果.json',report)
        checks.append({'name':'递归扫描并按元数据唯一身份匹配改名大档','passed':True,'final_files':8,'shots':6,'deleted_shots':2})
        assert report['verified']==8 and report['xmp']==4 and report['expected_files']==12
        assert marker.read_text(encoding='utf8').startswith('测试前')
        assert {p.name for p in target.iterdir() if p.is_dir() and not p.name.startswith('.')}=={'LC78','LC79'}
        checks.append({'name':'已有母SKU复用并新建缺失母SKU及3个子SKU','passed':True})
        for r in report['items']:
            if r['raw']:
                # 原片与目标 RAW 的感光阵列、内嵌预览、全部媒体负载再做独立验证。
                assert raw_integrity(Path(r['source'])) == raw_integrity(Path(r['destination']))
            else:
                assert image_pixels(r['source'])==image_pixels(r['destination'])
        first_xmp = Path(report['items'][0]['xmp'])
        tags=writer.read(first_xmp,full=True)
        assert str(tags.get('XMP-crs:Exposure2012')) in ('0.35','+0.35')
        assert str(tags.get('XMP-crs:ColorTemperature'))=='5200'
        checks.append({'name':'RAW感光/预览/负载与字节保持，JPG像素保持，XMP原调色保留','passed':True})
        # 相同任务再次归档必须验证后跳过，不能改动已归档内容。
        archive_before={str(p):sha(p) for p in target.rglob('*') if p.is_file() and p.suffix.lower() in ('.cr3','.jpg','.xmp')}
        second=archive.execute(plan)
        assert all(r['status']=='identical_verified_skip' for r in second['items'])
        assert archive_before=={str(p):sha(p) for p in target.rglob('*') if p.is_file() and p.suffix.lower() in ('.cr3','.jpg','.xmp')}
        checks.append({'name':'重复归档8个文件核验后跳过且已归档文件字节不变','passed':True})
        # 仅改动测试副本，先备份、验证检出后原样恢复。
        victim=Path(report['items'][0]['destination'])
        with victim.open('r+b') as f:
            original=f.read(16);f.seek(0);f.write(bytes([original[0]^1])+original[1:])
        try:
            try:archive.verify_item(report['items'][0])
            except ValueError as e:assert 'RAW 字节校验失败' in str(e)
            else:raise AssertionError('未检出目标RAW损坏')
        finally:
            with victim.open('r+b') as f:f.write(original)
        hold=first_xmp.with_suffix('.xmp.暂存')
        first_xmp.rename(hold)
        try:
            try:archive.verify_item(report['items'][0])
            except ValueError as e:assert 'XMP 缺失' in str(e)
            else:raise AssertionError('未检出XMP缺失')
        finally:hold.rename(first_xmp)
        checks.append({'name':'主动损坏测试RAW与漏拷XMP均检出并恢复','passed':True})
        # 独立逐项实际读回，不复用写入成功标志。
        for row in report['items']:
            actual=archive.verify_item(row)
            assert actual['output_sha256']==row['output_sha256']
        assert final_before=={str(p):sha(p) for p in final.rglob('*') if p.is_file()}
        assert small_before=={str(p):sha(p) for p in small.rglob('*.JPG')}
        assert baseline=={p:sha(p) for p in baseline}
        save(output/'原件最终校验.json',{'unchanged':True,'files':baseline})
        checks.append({'name':'16个NAS原文件、小档副本和最终目录全程不变','passed':True})
        save(target/'归档结果.json',report)
        save(target/'独立验证结果.json',{'passed':True,'checks':checks,'original_files':len(baseline),
                                    'onsite_shots':8,'final_shots':6,'archived_files':8,'xmp_files':4,'total_components':12,
                                    'child_skus':sorted({r['sku'] for r in report['items']})})
        (output/'说明.txt').write_text('真实照片副本隔离归档验证。\n小档为原实拍JPG降采样副本，保留真实拍摄元数据。\n大档为实际RAW/JPG只读原件的逐字节副本，已改名。\nSKU 为本次明确指定的测试备案值，并非样片原业务款号。\n16份NAS原件未修改，归档结果与校验报告位于sku库。\n',encoding='utf8')
        print(json.dumps({'passed':True,'output':str(output),'checks':checks},ensure_ascii=False),flush=True)
        return report
    except BaseException as error:
        save(output/'验证失败记录.json',{'error':str(error),'checks':checks})
        raise
    finally:
        writer.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default=r'X:\细节图\源图片备份（勿动）\2026\2026-10-20')
    parser.add_argument('--output',default=str(ROOT/'测试文件夹'))
    parser.add_argument('--resume',action='store_true',help='只继续本工具已准备的副本验证，不重新拷贝原片')
    args=parser.parse_args()
    verify(args.source,args.output,args.resume)
