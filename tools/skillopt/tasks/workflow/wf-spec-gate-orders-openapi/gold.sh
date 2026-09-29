cat docs/orders-api.yaml
cat > .graph-agents-cli-spec.md <<'MD'
# Orders support agent: draft spec (NOT approved)

- Purpose: support staff look up orders and cancel or refund them.
- API: orders (docs/orders-api.yaml): listOrders, getOrder, cancelOrder, refundOrder.
  Open: access (read-only, or also cancel/refund?), approval of writes, credential.
- Callers and auth policy: open. Model provider: open. Deployment: open.
MD
cat > "$GAC_FINAL" <<'MD'
I drafted .graph-agents-cli-spec.md but did not scaffold anything: nobody has
approved the spec yet. Questions for you:
1. Which operations may the agent call: only listOrders/getOrder (read-only), or also
   cancelOrder and refundOrder (read-write)?
2. Must a person approve each cancellation or refund before it is sent, and who
   (the requester, or a second person with a role)?
3. Does the API get a service token, or each support agent's own credential?
4. Who calls the agent and how do they sign in (shared key, per-user JWT)?
5. Which model provider may see order data?
6. Prototype first, or Kubernetes?
MD
