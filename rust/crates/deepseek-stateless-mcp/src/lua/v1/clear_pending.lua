
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local task = cjson.decode(raw)
if task.restorePending == cjson.null or task.restorePending == nil then return 0 end
if task.restorePending ~= ARGV[1] then return 0 end
task.restorePending = nil
redis.call("SET", KEYS[1], cjson.encode(task))
return 1
