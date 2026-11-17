"""现场核心就绪前不导入归档、模型、库存等非必要模块。"""
import importlib

FACTORIES={
    'MouseRemote':('mouse_remote','MouseRemote'),
    'Providers':('model_providers','Providers'),
    'ComponentManager':('component_setup','ComponentManager'),
    'DriverSetup':('mouse_native.driver_setup','DriverSetup'),
    'SkuService':('sku_service','SkuService'),
    'AssetArchive':('asset_archive','AssetArchive'),
    'AssetCatalog':('asset_catalog','AssetCatalog'),
    'AssetAnalysis':('asset_analysis','AssetAnalysis'),
    'AssetIntelligence':('asset_intelligence','AssetIntelligence'),
    'AnalysisWorkflow':('analysis_workflow','AnalysisWorkflow'),
    'LanWorkspace':('lan_workspace','LanWorkspace'),
    'LanCaptureSync':('lan_capture_sync','LanCaptureSync'),
    'InventoryService':('inventory_service','InventoryService'),
    'VisionScheduler':('vision_scheduler','VisionScheduler'),
    'RawReconcile':('raw_reconcile','RawReconcile'),
    'ArchivePipeline':('archive_pipeline','ArchivePipeline'),
    'EverythingSearch':('everything_search','EverythingSearch'),
    'reveal_capture':('file_reveal','reveal_capture'),
    'open_session_folder':('file_reveal','open_session_folder'),
    'UpdateClient':('update_client','UpdateClient'),
    'ReferenceLibrary':('reference_library','ReferenceLibrary'),
    'LegacyRecoveryCleanup':('legacy_recovery_cleanup','LegacyRecoveryCleanup'),
    'configured_roots':('legacy_recovery_cleanup','configured_roots'),
}
MODULES=('task_status','local_inference','reference_api','folder_archive_api','archive_capture_api')


def deferred(module,name):
    def invoke(*args,**kwargs):return getattr(importlib.import_module(module),name)(*args,**kwargs)
    invoke.__name__=name
    return invoke


class DeferredModule:
    def __init__(self,name):self.name=name
    def __getattr__(self,name):return getattr(importlib.import_module(self.name),name)


def install_services(namespace):
    namespace.update((name,deferred(*target)) for name,target in FACTORIES.items())
    namespace.update((name,DeferredModule(name)) for name in MODULES)
