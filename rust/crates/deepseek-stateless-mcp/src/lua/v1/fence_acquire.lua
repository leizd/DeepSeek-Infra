
if redis.call("GET", KEYS[2]) then return "backup-fenced" end
local raw = redis.call("GET", KEYS[1])
if raw then
  local fence = cjson.decode(raw)
  if fence.restoreId ~= ARGV[1] then return "fenced" end
  redis.call("PEXPIRE", KEYS[1], ARGV[3])
  return "ok"
end
redis.call("SET", KEYS[1], cjson.encode({restoreId = ARGV[1], createdAt = tonumber(ARGV[2]), expiresAt = tonumber(ARGV[2]) + tonumber(ARGV[3])}), "PX", ARGV[3])
return "ok"
