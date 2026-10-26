-- 仅解析 JSON，不执行 Lua；拒绝重复键、非法数字、尾随内容和过深结构。
local M = { null = {} }
local function utf8(n)
    if n < 128 then return string.char(n) end
    if n < 2048 then return string.char(192 + math.floor(n/64),128+n%64) end
    if n < 65536 then return string.char(224+math.floor(n/4096),128+math.floor(n/64)%64,128+n%64) end
    return string.char(240+math.floor(n/262144),128+math.floor(n/4096)%64,128+math.floor(n/64)%64,128+n%64)
end
function M.decode(text)
    assert(type(text)=='string' and #text<=32000000,'JSON 文件过大或格式错误')
    text=text:gsub('^\239\187\191',''); local pos=1;local parse
    local function ws() local _,last=text:find('^[ \r\n\t]*',pos);pos=(last or pos-1)+1 end
    local function str()
        assert(text:sub(pos,pos)=='"','需要 JSON 字符串');pos=pos+1;local result={}
        while pos<=#text do
            local ch=text:sub(pos,pos);pos=pos+1
            if ch=='"' then return table.concat(result) end
            if ch=='\\' then
                local code=text:sub(pos,pos);pos=pos+1
                local replacements={['"']='"',['\\']='\\',['/']='/',b='\b',f='\f',n='\n',r='\r',t='\t'}
                if replacements[code] then table.insert(result,replacements[code])
                elseif code=='u' then
                    local hex=text:sub(pos,pos+3);assert(hex:match('^%x%x%x%x$'),'非法 Unicode');local n=tonumber(hex,16);pos=pos+4
                    if n>=0xD800 and n<=0xDBFF then
                        assert(text:sub(pos,pos+1)=='\\u','缺少低代理项');local low=tonumber(text:sub(pos+2,pos+5),16);assert(low and low>=0xDC00 and low<=0xDFFF,'低代理项非法');n=65536+(n-0xD800)*1024+low-0xDC00;pos=pos+6
                    else assert(n<0xDC00 or n>0xDFFF,'孤立低代理项') end
                    table.insert(result,utf8(n))
                else error('非法 JSON 转义') end
            else assert(ch:byte()>=32,'JSON 控制字符非法');table.insert(result,ch) end
        end
        error('字符串未闭合')
    end
    parse=function(depth)
        assert(depth<=64,'JSON 嵌套过深');ws();local ch=text:sub(pos,pos)
        if ch=='"' then return str() end
        if ch=='{' then
            pos=pos+1;ws();local result={};if text:sub(pos,pos)=='}' then pos=pos+1;return result end
            while true do
                ws();local key=str();assert(result[key]==nil,'重复 JSON 键');ws();assert(text:sub(pos,pos)==':','缺少冒号');pos=pos+1;result[key]=parse(depth+1);ws();local sep=text:sub(pos,pos);pos=pos+1
                if sep=='}' then return result end;assert(sep==',','缺少对象分隔符')
            end
        end
        if ch=='[' then
            pos=pos+1;ws();local result={};if text:sub(pos,pos)==']' then pos=pos+1;return result end
            while true do table.insert(result,parse(depth+1));ws();local sep=text:sub(pos,pos);pos=pos+1;if sep==']' then return result end;assert(sep==',','缺少数组分隔符') end
        end
        for _,entry in ipairs({{'true',true},{'false',false},{'null',M.null}}) do
            if text:sub(pos,pos+#entry[1]-1)==entry[1] then pos=pos+#entry[1];return entry[2] end
        end
        local start=pos
        if ch=='-' then pos=pos+1 end
        if text:sub(pos,pos)=='0' then pos=pos+1 else local digits=text:match('^%d+',pos);assert(digits,'非法 JSON 值');pos=pos+#digits end
        if text:sub(pos,pos)=='.' then pos=pos+1;local digits=text:match('^%d+',pos);assert(digits,'非法小数');pos=pos+#digits end
        if text:sub(pos,pos):lower()=='e' then pos=pos+1;if text:sub(pos,pos):match('[+-]') then pos=pos+1 end;local digits=text:match('^%d+',pos);assert(digits,'非法指数');pos=pos+#digits end
        local number=tonumber(text:sub(start,pos-1));assert(number and number==number and number~=math.huge and number~=-math.huge,'数字溢出');return number
    end
    local result=parse(0);ws();assert(pos>#text,'JSON 尾随内容');return result
end
function M.encode(value)
    local kind=type(value)
    if value==M.null or value==nil then return 'null' end
    if kind=='boolean' or kind=='number' then return tostring(value) end
    if kind=='string' then return '"'..value:gsub('[%z\1-\31\\"]',function(ch)local map={['"']='\\"',['\\']='\\\\',['\n']='\\n',['\r']='\\r',['\t']='\\t'};return map[ch] or string.format('\\u%04x',ch:byte()) end)..'"' end
    assert(kind=='table','无法编码 JSON 类型');local result={}
    if #value>0 then for _,v in ipairs(value) do table.insert(result,M.encode(v)) end;return '['..table.concat(result,',')..']' end
    for k,v in pairs(value) do assert(type(k)=='string','JSON 键必须是字符串');table.insert(result,M.encode(k)..':'..M.encode(v)) end;return '{'..table.concat(result,',')..'}'
end
return M
