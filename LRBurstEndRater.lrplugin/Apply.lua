local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'

local BurstCore = require 'BurstCore'

LrTasks.startAsyncTask(function()
    local result, message = BurstCore.analyzeSelected()
    if not result then
        LrDialogs.message('连拍末张标星', message, 'warning')
        return
    end

    BurstCore.apply(result)
    local details = BurstCore.summary(result, true)
        .. '\n\n本次新增 1 星：' .. tostring(result.changedToOne)
        .. '\n本次清除非末张 1 星：' .. tostring(result.clearedOne)
        .. '\n\n若需撤销，立即按 Ctrl+Z。'
    LrDialogs.message('连拍末张标星', details, 'info')
end)
