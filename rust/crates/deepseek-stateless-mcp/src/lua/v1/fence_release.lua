
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local fence = cjson.decode(raw)
if fence.restoreId ~= ARGV[1] then return 0 end
redis.call("DEL", KEYS[1])
return 1
