
local existing_id = redis.call("GET", KEYS[1])
if existing_id then
  local existing = redis.call("GET", ARGV[3] .. existing_id)
  return {"existing", existing or ""}
end
if redis.call("EXISTS", KEYS[4]) == 1 then return redis.error_reply("BACKUP_FENCED") end
if redis.call("EXISTS", KEYS[6]) == 1 then return redis.error_reply("RESTORE_FENCED") end
redis.call("SET", KEYS[1], ARGV[1])
redis.call("SET", KEYS[2], ARGV[2])
redis.call("ZADD", KEYS[3], ARGV[4], ARGV[1])
redis.call("INCR", KEYS[5])
return {"created", ARGV[2]}
