
local raw = redis.call("GET", KEYS[1])
if not raw then return "missing" end
local journal = cjson.decode(raw)
if journal.transactionDigest ~= ARGV[1] then return "digest" end
if journal.phase == "committed-pending-complete" then return cjson.encode(journal) end
if journal.phase ~= "committing" then return "phase:" .. tostring(journal.phase) end
redis.call("SET", KEYS[2], journal.restoreEpoch)
if journal.imported > 0 then redis.call("INCR", KEYS[3]) end
journal.phase = "committed-pending-complete"
journal.updatedAt = tonumber(ARGV[2])
redis.call("SET", KEYS[1], cjson.encode(journal), "PX", ARGV[3])
return cjson.encode(journal)
