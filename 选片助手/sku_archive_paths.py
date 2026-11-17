"""归档范围内的款号目录识别与已有层级复用。"""
from pathlib import Path


def directory_sku(path, root, parent_length, guess):
    root=Path(root);folder=Path(path).parent
    for candidate in (folder,*folder.parents):
        if not candidate.is_relative_to(root):break
        # 只接受完整的目录名，不拿文件名、日期或目录说明中的片段猜款号。
        sku=guess(str(Path(candidate.name)/'photo-0000.jpg'))[0]
        if sku and sku.casefold()==candidate.name.casefold() and len(sku)>parent_length:
            return sku
    return ''


def sku_directory(root, sku, parent_length=4):
    root=Path(root)
    if root.name.casefold()==sku.casefold():return root
    parent=sku[:parent_length]
    return (root if root.name.casefold()==parent.casefold() else root/parent)/sku


def arranged_source(source, root, sku, parent_length=4):
    source,root=Path(source),Path(root)
    for folder in (source.parent,*source.parent.parents):
        if not folder.is_relative_to(root):break
        if folder.name.casefold()==sku.casefold():return source
    return sku_directory(root,sku,parent_length)/source.name
