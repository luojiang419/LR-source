return {
    LrSdkVersion = 14.0,
    LrSdkMinimumVersion = 14.0,
    LrToolkitIdentifier = 'com.codex.lr-burst-end-rater',
    LrPluginName = '连拍末张标星',

    LrLibraryMenuItems = {
        {
            title = '预览当前选择（自动识别连拍间隔）',
            file = 'Preview.lua',
            enabledWhen = 'photosSelected',
        },
        {
            title = '执行：连拍只标最后一张（自动阈值）',
            file = 'Apply.lua',
            enabledWhen = 'photosSelected',
        },
    },

    VERSION = {
        major = 1,
        minor = 1,
        revision = 1,
        build = 1,
    },
}
