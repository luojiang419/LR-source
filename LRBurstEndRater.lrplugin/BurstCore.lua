local LrApplication = import 'LrApplication'

local M = {}

M.targetRating = 1
M.fallbackThresholdSeconds = 3
M.maximumTrainingGapSeconds = 120
M.minimumThresholdSeconds = 1
M.maximumThresholdSeconds = 15

local function normalizeStem(path)
    local normalized = string.lower(string.gsub(path or '', '/', '\\'))
    return string.gsub(normalized, '%.[^%.\\]+$', '')
end

local function parseIsoTime(value)
    if type(value) ~= 'string' then
        return nil
    end

    local year, month, day, hour, minute, second = string.match(
        value,
        '^(%d%d%d%d)%D(%d%d)%D(%d%d)%D(%d%d)%D(%d%d)%D(%d%d)'
    )

    if not year then
        return nil
    end

    return os.time {
        year = tonumber(year),
        month = tonumber(month),
        day = tonumber(day),
        hour = tonumber(hour),
        min = tonumber(minute),
        sec = tonumber(second),
    }
end

local function captureTime(metadata)
    if type(metadata.dateTimeOriginal) == 'number' then
        return metadata.dateTimeOriginal
    end

    return parseIsoTime(metadata.dateTimeOriginalISO8601)
end

local function readMetadata(catalog, photos)
    local keys = {
        'path',
        'dateTimeOriginal',
        'dateTimeOriginalISO8601',
        'rating',
    }

    return catalog:batchGetRawMetadata(photos, keys)
end

local function inferThreshold(shots)
    local values = {}
    local minimum = nil
    local maximum = nil

    for index = 2, #shots do
        local gap = shots[index].time - shots[index - 1].time
        if gap >= 0 and gap <= M.maximumTrainingGapSeconds then
            local adjustedGap = math.max(gap, 0.5)
            local value = math.log(adjustedGap)
            table.insert(values, value)
            minimum = minimum and math.min(minimum, value) or value
            maximum = maximum and math.max(maximum, value) or value
        end
    end

    if #values < 20 or not minimum or not maximum or minimum == maximum then
        return M.fallbackThresholdSeconds, nil, nil, true
    end

    local centerA = minimum
    local centerB = maximum
    local countA = 0
    local countB = 0

    for _ = 1, 40 do
        local sumA = 0
        local sumB = 0
        countA = 0
        countB = 0

        for _, value in ipairs(values) do
            if math.abs(value - centerA) <= math.abs(value - centerB) then
                sumA = sumA + value
                countA = countA + 1
            else
                sumB = sumB + value
                countB = countB + 1
            end
        end

        if countA == 0 or countB == 0 then
            return M.fallbackThresholdSeconds, nil, nil, true
        end

        local nextA = sumA / countA
        local nextB = sumB / countB
        local movement = math.abs(nextA - centerA) + math.abs(nextB - centerB)
        centerA = nextA
        centerB = nextB

        if movement < 0.000001 then
            break
        end
    end

    if centerA > centerB then
        centerA, centerB = centerB, centerA
        countA, countB = countB, countA
    end

    local shortCenter = math.exp(centerA)
    local longCenter = math.exp(centerB)
    local smallerCluster = math.min(countA, countB)

    if longCenter / shortCenter < 1.8 or smallerCluster < math.max(5, #values * 0.05) then
        return M.fallbackThresholdSeconds, shortCenter, longCenter, true
    end

    local boundary = math.exp((centerA + centerB) / 2)
    local threshold = math.floor(boundary + 0.5)
    threshold = math.max(M.minimumThresholdSeconds, math.min(M.maximumThresholdSeconds, threshold))
    return threshold, shortCenter, longCenter, false
end

function M.analyzeSelected()
    local catalog = LrApplication.activeCatalog()
    local photos = catalog:getTargetPhotos()

    if not photos or #photos < 2 then
        return nil, '请在图库模块中打开目标文件夹，按 Ctrl+A 全选后再运行。'
    end

    local metadataByPhoto = readMetadata(catalog, photos)
    local shotByStem = {}
    local missingTime = 0

    for _, photo in ipairs(photos) do
        local metadata = metadataByPhoto[photo] or {}
        local path = metadata.path
        local time = captureTime(metadata)

        if path and time then
            local stem = normalizeStem(path)
            local shot = shotByStem[stem]
            if not shot then
                shot = {
                    stem = stem,
                    time = time,
                    items = {},
                }
                shotByStem[stem] = shot
            elseif time < shot.time then
                shot.time = time
            end

            table.insert(shot.items, {
                photo = photo,
                rating = metadata.rating,
                path = path,
            })
        else
            missingTime = missingTime + 1
        end
    end

    local shots = {}
    for _, shot in pairs(shotByStem) do
        table.insert(shots, shot)
    end

    table.sort(shots, function(a, b)
        if a.time == b.time then
            return a.stem < b.stem
        end
        return a.time < b.time
    end)

    local thresholdSeconds, shortCenter, longCenter, usedFallback = inferThreshold(shots)

    local keeperShots = 0
    local discardedShots = 0
    local keeperItems = 0
    local discardedItems = 0
    local protectedHighRatings = 0

    for index, shot in ipairs(shots) do
        local nextShot = shots[index + 1]
        shot.keeper = not nextShot or (nextShot.time - shot.time) > thresholdSeconds

        if shot.keeper then
            keeperShots = keeperShots + 1
            keeperItems = keeperItems + #shot.items
        else
            discardedShots = discardedShots + 1
            discardedItems = discardedItems + #shot.items
        end

        for _, item in ipairs(shot.items) do
            if item.rating and item.rating >= 2 then
                protectedHighRatings = protectedHighRatings + 1
            end
        end
    end

    return {
        catalog = catalog,
        selectedItems = #photos,
        shots = shots,
        shotCount = #shots,
        keeperShots = keeperShots,
        discardedShots = discardedShots,
        keeperItems = keeperItems,
        discardedItems = discardedItems,
        protectedHighRatings = protectedHighRatings,
        missingTime = missingTime,
        thresholdSeconds = thresholdSeconds,
        shortCenter = shortCenter,
        longCenter = longCenter,
        usedFallback = usedFallback,
    }
end

function M.summary(result, applied)
    local heading = applied and '已完成标星' or '标星预览'
    local lines = {
        heading,
        '',
        '当前选择的目录项：' .. tostring(result.selectedItems),
        '识别到的拍摄编号（RAW/JPG 合并）：' .. tostring(result.shotCount),
        '保留并标 1 星：' .. tostring(result.keeperShots) .. ' 组 / ' .. tostring(result.keeperItems) .. ' 个目录项',
        '连拍前帧（不标星）：' .. tostring(result.discardedShots) .. ' 组 / ' .. tostring(result.discardedItems) .. ' 个目录项',
        '自动识别的连拍阈值：' .. tostring(result.thresholdSeconds) .. ' 秒。',
        '分组规则：相邻拍摄间隔 <= 阈值视为连拍，只保留末张。',
        '保护规则：已有 2–5 星不降级；非保留张只会清除 1 星。',
    }

    if result.shortCenter and result.longCenter then
        table.insert(lines, string.format(
            '拍摄节奏中心：快速 %.1f 秒 / 慢速 %.1f 秒。',
            result.shortCenter,
            result.longCenter
        ))
    end

    if result.usedFallback then
        table.insert(lines, '当前数据未形成足够清晰的双节奏，已使用保守备用阈值。')
    end

    if result.protectedHighRatings > 0 then
        table.insert(lines, '已保护现有 2–5 星目录项：' .. tostring(result.protectedHighRatings))
    end

    if result.missingTime > 0 then
        table.insert(lines, '缺少路径或拍摄时间，未处理：' .. tostring(result.missingTime))
    end

    return table.concat(lines, '\n')
end

function M.apply(result)
    local changedToOne = 0
    local clearedOne = 0

    result.catalog:withWriteAccessDo('连拍末张标星（自动阈值）', function()
        for _, shot in ipairs(result.shots) do
            for _, item in ipairs(shot.items) do
                local current = item.photo:getRawMetadata('rating')

                if shot.keeper then
                    if not current or current < M.targetRating then
                        item.photo:setRawMetadata('rating', M.targetRating)
                        changedToOne = changedToOne + 1
                    end
                elseif current == M.targetRating then
                    item.photo:setRawMetadata('rating', nil)
                    clearedOne = clearedOne + 1
                end
            end
        end
    end, { timeout = 60 })

    result.changedToOne = changedToOne
    result.clearedOne = clearedOne
    return result
end

return M
