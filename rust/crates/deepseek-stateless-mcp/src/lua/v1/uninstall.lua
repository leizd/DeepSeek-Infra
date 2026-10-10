
local existing = redis.call("GET", KEYS[1])
if existing and existing == ARGV[1] then
  redis.call("DEL", KEYS[1])
end
local indexed = redis.call("GET", KEYS[2])
if indexed and indexed == ARGV[2] then
  redis.call("DEL", KEYS[2])
end
return 1
