
local raw = redis.call("GET", KEYS[1])
if not raw then return "missing" end
local journal = cjson.decode(raw)
local expected = cjson.decode(ARGV[1])
local matched = false
for _, phase in ipairs(expected) do
  if journal.phase == phase then matched = true end
end
if not matched then return "phase:" .. tostring(journal.phase) end
if ARGV[3] ~= "" and journal.transactionDigest ~= ARGV[3] then return "digest" end
journal.phase = ARGV[2]
journal.updatedAt = tonumber(ARGV[4])
redis.call("SET", KEYS[1], cjson.encode(journal), "PX", ARGV[5])
return cjson.encode(journal)
