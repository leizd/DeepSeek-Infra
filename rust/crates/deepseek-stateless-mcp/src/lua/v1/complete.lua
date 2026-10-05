
local raw = redis.call("GET", KEYS[1])
if not raw then return "" end
local task = cjson.decode(raw)
if task.status ~= "running" or task.ownerInstance ~= ARGV[1] then return "" end
if redis.call("EXISTS", KEYS[4]) == 1 then return "FENCED" end
local outcome = cjson.decode(ARGV[2])
if outcome.error == cjson.null and outcome.exitCode == 0 then
  task.status = "succeeded"
else
  task.status = "failed"
end
task.stdout = outcome.stdout
task.stderr = outcome.stderr
task.exitCode = outcome.exitCode
task.error = outcome.error
task.leaseUntil = cjson.null
task.updatedAt = tonumber(ARGV[3])
local encoded = cjson.encode(task)
redis.call("SET", KEYS[1], encoded)
redis.call("ZREM", KEYS[2], task.id)
redis.call("INCR", KEYS[3])
return encoded
