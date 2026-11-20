package media

// This is a bound on the typed protobuf representation in Rust staging, not a
// new public input limit. Public JSON keeps 16,000,000 bytes. Chunking avoids
// coupling media content size to historical 4 MiB control/receipt rows.
const MaxCreatePayloadBytes = 128 * 1024 * 1024
const CreateChunkBytes = 256 * 1024
