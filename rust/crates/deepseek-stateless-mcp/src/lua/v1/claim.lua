
if redis.call("EXISTS", KEYS[2]) == 1 then return "" end
if redis.call("EXISTS", KEYS[4]) == 1 then return "" end
local ids = redis.call("ZRANGEBYSCORE", KEYS[1], "-inf", ARGV[1], "LIMIT", 0, 16)
for _, id in ipairs(ids) do
  local key = ARGV[4] .. id
  local raw = redis.call("GET", key)
  if not raw then
    redis.call("ZREM", KEYS[1], id)
  else
    local task = cjson.decode(raw)
    local claimable = task.status == "queued"
    if task.status == "running" and task.leaseUntil ~= cjson.null and task.leaseUntil <= tonumber(ARGV[1]) then
      claimable = true
    end
    if claimable then
      task.status = "running"
      task.ownerInstance = ARGV[2]
      task.leaseUntil = tonumber(ARGV[1]) + tonumber(ARGV[3])
      task.attempts = task.attempts + 1
      task.updatedAt = tonumber(ARGV[1])
      task.error = cjson.null
      local encoded = cjson.encode(task)
      redis.call("SET", key, encoded)
      redis.call("ZADD", KEYS[1], task.leaseUntil, id)
      redis.call("INCR", KEYS[3])
      return encoded
    end
  end
end
return ""
