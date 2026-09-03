# Assistant fork core

This directory contains contracts owned by our Odysseus fork.  v0.2.0 does **not**
yet replace the live Odysseus agent loop; it creates the stable seam we will wire
into it incrementally.

Core invariants:

1. **Model reasoning is not authority.** Tool execution and permissions stay in Python.
2. **Tool visibility is not permission.** The broker only selects among tools a controller already permitted.
3. **Embeddings are ranking, not availability.** A cold/failed vector service may degrade ranking but must not erase critical capabilities.
4. **Conversation state is typed.** A calendar follow-up stays a calendar follow-up because the capability is active, not because a regex found the word `calendar` again.
5. **There is a discovery recovery path.** If automatic routing misses, the model can discover/request an allowed capability instead of getting stuck.
6. **Agent identity is provenance.** `agent_id` / `delegated_by` prepare for future sub-agents but grant no rights by themselves.
7. **Provider capabilities should become explicit.** Native tool calling, thinking behavior and streaming quirks belong in provider adapters/config, not scattered URL/model-name guesses.

Run the current no-dependency contracts:

```bash
./fork.sh self-test
```
