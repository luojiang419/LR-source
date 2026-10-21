local LrApplication=import 'LrApplication'
local LrDialogs=import 'LrDialogs'
local LrFileUtils=import 'LrFileUtils'
local LrPathUtils=import 'LrPathUtils'
local LrTasks=import 'LrTasks'
local LrHttp=import 'LrHttp'
local LrPrefs=import 'LrPrefs'
local Json=require 'Json'
local M={connected=false,lastUndo=nil,lastDiagnostic=nil}
local base='http://127.0.0.1:8769'
local prefs=LrPrefs.prefsForPlugin()

local function normalize(path)return string.lower((path:gsub('\\','/'):gsub('/+$',''))) end
local function rootPath(path)
    assert(type(path)=='string' and (#path<32768),'源文件夹非法')
    assert(path:match('^%a:[/\\]') or path:match('^[/\\][/\\][^/\\]+[/\\][^/\\]+'),'请选择本机或 UNC 绝对目录')
    assert(not path:find('[%z\1-\31]'),'路径含控制字符');return path
end
local function child(root,relative)
    assert(type(relative)=='string' and #relative>0 and #relative<32768,'相对路径非法')
    assert(not relative:find('\\') and not relative:find(':') and relative:sub(1,1)~='/' and not relative:find('[%z\1-\31]'),'禁止绝对或特殊路径')
    local result=root
    for part in (relative..'/'):gmatch('(.-)/') do assert(part~='' and part~='.' and part~='..','禁止越界路径');result=LrPathUtils.child(result,part) end
    local prefix=normalize(root)..'/'
    assert(normalize(result):sub(1,#prefix)==prefix,'路径超出源目录')
    return result
end

function M.validate(manifest,overrideRoot)
    assert(type(manifest)=='table' and manifest.schema=='framekeep.selection' and manifest.version==2,'仅支持正式版 framekeep.selection v2 清单')
    assert(manifest.demo==false,'拒绝演示清单')
    assert(type(manifest.source)=='table' and type(manifest.captures)=='table','清单缺少源目录或照片')
    assert(type(manifest.applyPolicy)=='table' and manifest.applyPolicy.clearUnlisted==false,'不允许改动未列出的照片')
    assert(manifest.applyPolicy.mode=='selected-only' or manifest.applyPolicy.mode=='include-unselected','未知应用模式')
    assert(#manifest.captures<=100000,'清单照片过多')
    local root=rootPath(overrideRoot or manifest.source.root);local entries={};local seen={}
    for _,capture in ipairs(manifest.captures) do
        assert(type(capture)=='table' and type(capture.rating)=='number' and capture.rating%1==0 and capture.rating>=0 and capture.rating<=5,'星级必须为 0–5 整数')
        assert(type(capture.relativePaths)=='table' and #capture.relativePaths>=1 and #capture.relativePaths<=4,'RAW/JPG 路径非法')
        assert(manifest.applyPolicy.mode~='selected-only' or capture.rating>0,'选中项清单不得清零')
        local paths={}
        for _,relative in ipairs(capture.relativePaths) do
            local path=child(root,relative);local key=normalize(path);assert(not seen[key],'清单存在重复路径');seen[key]=true;table.insert(paths,path)
        end
        table.insert(entries,{paths=paths,rating=capture.rating})
    end
    return {root=root,entries=entries}
end

function M.plan(manifest,overrideRoot)
    local validated=M.validate(manifest,overrideRoot);local catalog=LrApplication.activeCatalog();local seen={};local plan={catalog=catalog,root=validated.root,changes={},missing=0,companions=0,protected=0,unchanged=0,matched=0}
    local aliases={validated.root}
    if not overrideRoot and type(manifest.source.pathAliases)=='table' then
        assert(#manifest.source.pathAliases<=27,'路径别名过多')
        for _,alias in ipairs(manifest.source.pathAliases) do rootPath(alias);table.insert(aliases,alias) end
    end
    for _,capture in ipairs(validated.entries) do
        local found=0;local absent=0
        for _,path in ipairs(capture.paths) do
            local photo=catalog:findPhotoByPath(path)
            if not photo and #aliases>1 then
                local relative=path:sub(#validated.root+2)
                for _,alias in ipairs(aliases) do photo=catalog:findPhotoByPath(child(alias,relative));if photo then break end end
            end
            if photo then
                found=found+1
                local key=normalize(photo:getRawMetadata('path') or path)
                if not seen[key] then
                    seen[key]=true;plan.matched=plan.matched+1;local before=photo:getRawMetadata('rating') or 0
                    if before==capture.rating then plan.unchanged=plan.unchanged+1
                    elseif before>=2 then plan.protected=plan.protected+1
                    else table.insert(plan.changes,{photo=photo,path=path,before=before,after=capture.rating}) end
                end
            else absent=absent+1 end
        end
        if found==0 then plan.missing=plan.missing+1 else plan.companions=plan.companions+absent end
    end
    return plan
end

function M.diagnose(manifest)
    local catalog=LrApplication.activeCatalog()
    local photos=catalog:getAllPhotos()
    local metadata=catalog:batchGetRawMetadata(photos,{'path'})
    local folderName=manifest and manifest.source and manifest.source.folderName
    local roots={};local samples={};local selected=catalog:getTargetPhoto()
    local selectedPath=selected and selected:getRawMetadata('path') or nil
    local keys={};local marker=nil
    if folderName and not folderName:find('[/\\]') then
        marker='/'..string.lower(folderName)..'/'
        for _,capture in ipairs(manifest.captures) do for _,relative in ipairs(capture.relativePaths) do
            keys[string.lower(relative)]=true
        end end
    end
    -- 只访问目录路径元数据，不读取或渲染 RAW。
    for _,photo in ipairs(photos) do
        local path=(metadata[photo] or {}).path
        if path then
            local norm=normalize(path)
            local pos=marker and norm:find(marker,1,true)
            if pos then
                local rootEnd=pos+#marker-2;local candidate=path:sub(1,rootEnd)
                local relative=norm:sub(pos+#marker)
                local key=normalize(candidate)
                local item=roots[key] or {path=candidate,count=0,matchedPaths=0};item.count=item.count+1
                if keys[relative] then item.matchedPaths=item.matchedPaths+1 end
                roots[key]=item
                if #samples<6 then table.insert(samples,path) end
            end
        end
    end
    local directories={};for _,item in pairs(roots) do table.insert(directories,item) end
    table.sort(directories,function(a,b)return a.matchedPaths>b.matchedPaths end)
    local diagnostic={catalogPhotos=#photos,currentPhotoPath=selectedPath or '',directories=directories,samples=samples,sourceFolder=folderName or ''}
    M.lastDiagnostic=diagnostic
    local body=LrHttp.get(base..'/bridge/hello',nil,10)
    if body then
        local hello=Json.decode(body)
        if hello.token then LrHttp.post(base..'/bridge/diagnostics',Json.encode(diagnostic),{{field='Content-Type',value='application/json'},{field='X-Framekeep-Bridge',value=hello.token}},'POST',10) end
    end
    return directories
end

function M.summary(plan)
    return table.concat({'源目录：'..plan.root,'','匹配目录照片：'..plan.matched,'将修改星级：'..#plan.changes,'已有 2–5 星保护：'..plan.protected,'星级已一致：'..plan.unchanged,'未导入/未匹配拍摄编号：'..plan.missing,'未独立匹配的配对文件：'..plan.companions,'','仅修改 Lightroom 目录星级，不写原图或 XMP。'},'\n')
end

function M.apply(plan)
    local undo={};local changed=0;local conflicts=0
    plan.catalog:withWriteAccessDo('末帧：同步选片星级',function()
        for _,item in ipairs(plan.changes) do
            local current=item.photo:getRawMetadata('rating') or 0
            if current~=item.before or current>=2 then conflicts=conflicts+1
            else
                item.photo:setRawMetadata('rating',item.after>0 and item.after or nil)
                table.insert(undo,item);changed=changed+1
            end
        end
    end,{timeout=60})
    M.lastUndo={catalog=plan.catalog,items=undo}
    return {changed=changed,conflicts=conflicts,matched=plan.matched,protected=plan.protected,missing=plan.missing,companions=plan.companions}
end

function M.process(manifest)
    local plan=M.plan(manifest)
    if plan.matched==0 then
        local candidates=M.diagnose(manifest)
        local candidate=nil;local ambiguous=false
        for _,item in ipairs(candidates) do
            if item.matchedPaths>0 then if candidate then ambiguous=true else candidate=item.path end end
        end
        if candidate and not ambiguous then plan=M.plan(manifest,candidate) end
    end
    if plan.matched==0 then
        if LrDialogs.confirm('没有匹配的 LR 照片',M.summary(plan)..'\n\n连接正常，但 LR 目录未找到这些路径。若已导入，请选择 LR 中实际存放同一批照片的对应文件夹；否则先导入照片。','选择映射目录','退出')~='ok' then return 'cancelled',{missing=plan.missing} end
        local chosen=LrDialogs.runOpenPanel{title='选择已导入 LR 的对应照片目录',canChooseFiles=false,canChooseDirectories=true,allowsMultipleSelection=false}
        if not chosen or not chosen[1] then return 'cancelled',{} end
        plan=M.plan(manifest,chosen[1])
    end
    if LrDialogs.confirm('预览末帧选片同步',M.summary(plan),'写入星级','取消')~='ok' then return 'cancelled',{} end
    local result=M.apply(plan)
    LrDialogs.message('末帧同步完成','已修改 '..result.changed..' 张；保护 '..result.protected..' 张；未匹配 '..result.missing..' 个编号。\n可立即 Ctrl+Z，或运行“撤销上次末帧同步”。','info')
    return 'applied',result
end

function M.undo()
    local batch=M.lastUndo
    if not batch or batch.catalog~=LrApplication.activeCatalog() then LrDialogs.message('末帧桥接','当前目录没有可撤销的末帧同步。','info');return end
    local changed=0;local conflicts=0
    batch.catalog:withWriteAccessDo('末帧：撤销上次同步',function()
        for _,item in ipairs(batch.items) do
            if (item.photo:getRawMetadata('rating') or 0)==item.after then item.photo:setRawMetadata('rating',item.before>0 and item.before or nil);changed=changed+1 else conflicts=conflicts+1 end
        end
    end,{timeout=60})
    M.lastUndo=nil;LrDialogs.message('末帧撤销完成','恢复 '..changed..' 张；跳过已被人工修改的 '..conflicts..' 张。','info')
end

function M.importFile()
    local files=LrDialogs.runOpenPanel{title='选择 .framekeep.json 选片清单',canChooseFiles=true,canChooseDirectories=false,allowsMultipleSelection=false,fileTypes={'json'}}
    if files and files[1] then return M.process(Json.decode(LrFileUtils.readFile(files[1]))) end
end

function M.connect(silent)
    if M.connected then if not silent then LrDialogs.message('末帧桥接','连接任务已在运行；服务重启时会自动重连。','info') end;return end
    M.connected=true;local failures=0;local token=nil;local warned=false
    while M.connected do
        local ok,message=LrTasks.pcall(function()
            if not token then
                local body=LrHttp.get(base..'/bridge/hello',nil,10);assert(body,'请先启动末帧工作台')
                local hello=Json.decode(body);assert(hello.schema=='framekeep.bridge' and type(hello.token)=='string','工作台握手失败');token=hello.token
            end
            local headers={{field='X-Framekeep-Bridge',value=token}}
            local response=LrHttp.get(base..'/bridge/next',headers,10);assert(response,'工作台连接中断')
            local data=Json.decode(response)
            assert(data.error==nil,'工作台认证已变化，重新握手')
            if type(data.job)=='table' and data.job~=Json.null then
                local status,result
                -- 回传失败重试时不重复显示同一任务的写星对话框。
                if prefs.lastJobId==data.job.id and type(prefs.lastJobResult)=='string' then
                    local prior=Json.decode(prefs.lastJobResult);status=prior.status;result=prior.result
                else
                    local success,failure=LrTasks.pcall(function()status,result=M.process(data.job.manifest) end)
                    if not success then status='failed';result={message=tostring(failure):sub(1,500)};LrDialogs.message('末帧同步失败',result.message,'warning') end
                    prefs.lastJobResult=Json.encode{status=status,result=result};prefs.lastJobId=data.job.id
                end
                local postHeaders={{field='Content-Type',value='application/json'},{field='X-Framekeep-Bridge',value=token}}
                assert(LrHttp.post(base..'/bridge/result',Json.encode{id=data.job.id,status=status,result=result},postHeaders,'POST',10),'回传同步结果失败')
            end
        end)
        if ok then failures=0;prefs.lastConnectionError=nil else
            failures=failures+1;token=nil;prefs.lastConnectionError=tostring(message):sub(1,500)
            if failures==1 and not silent and not warned then warned=true;LrDialogs.message('末帧工作台暂未连接','请启动 http://127.0.0.1:8769 工作台。插件会继续在后台重连，可在菜单中断开。','info') end
        end
        if M.connected then LrTasks.sleep(math.min(10,2+failures*2)) end
    end
end
function M.stop()M.connected=false end
return M
