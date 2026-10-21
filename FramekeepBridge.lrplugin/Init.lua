local LrTasks=import 'LrTasks'
LrTasks.startAsyncTask(function()LrTasks.pcall(function()require('BridgeCore').connect(true) end)end)
