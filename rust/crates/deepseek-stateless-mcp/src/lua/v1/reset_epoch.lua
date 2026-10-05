
local raw = redis.call("GET", KEYS[1])
if raw and raw == ARGV[1] then
  redis.call("SET", KEYS[1], ARGV[2])
end
return 1
