
local existing = redis.call("GET", KEYS[1])
if existing then
  if existing ~= ARGV[1] then return "conflict" end
else
  redis.call("SET", KEYS[1], ARGV[1])
end
local indexed = redis.call("GET", KEYS[2])
if indexed then
  if indexed ~= ARGV[2] then return "conflict" end
else
  redis.call("SET", KEYS[2], ARGV[2])
end
redis.call("SADD", KEYS[3], KEYS[1])
redis.call("SADD", KEYS[4], KEYS[2])
return "ok"
