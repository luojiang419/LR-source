return {
    LrSdkVersion = 14.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = 'com.framekeep.lightroom.bridge',
    LrPluginName = '末帧选片桥接',
    LrInitPlugin = 'Init.lua',
    LrShutdownPlugin = 'Shutdown.lua',
    LrLibraryMenuItems = {
        { title = '连接末帧工作台（自动重连）', file = 'Connect.lua' },
        { title = '导入末帧选片清单…', file = 'Import.lua' },
        { title = '撤销上次末帧同步', file = 'Undo.lua' },
        { title = '断开末帧工作台', file = 'Disconnect.lua' },
    },
    VERSION = { major = 1, minor = 0, revision = 0, build = 1 },
}
