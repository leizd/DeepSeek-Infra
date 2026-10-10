
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local task = cjson.decode(raw)
if task.status ~= "running" or task.ownerInstance ~= ARGV[1] then return 0 end
task.leaseUntil = tonumber(ARGV[2]) + tonumber(ARGV[3])
task.updatedAt = tonumber(ARGV[2])
redis.call("SET", KEYS[1], cjson.encode(task))
redis.call("ZADD", KEYS[2], task.leaseUntil, task.id)
return 1
