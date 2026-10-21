local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'

local BurstCore = require 'BurstCore'

LrTasks.startAsyncTask(function()
    local result, message = BurstCore.analyzeSelected()
    if not result then
        LrDialogs.message('连拍末张标星', message, 'warning')
        return
    end

    LrDialogs.message('连拍末张标星', BurstCore.summary(result, false), 'info')
end)
