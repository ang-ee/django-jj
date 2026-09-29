# client/ — `jj-django`

The Rust workspace for the jj client lands here in milestone M1
([SPEC § 14](../docs/SPEC.md#14-the-client-jj-django)).

Planned layout:

```
client/
├── Cargo.toml            # workspace; jj-lib = "=0.45.1", jj-cli = "=0.45.1"
├── jj-django-store/      # the only crate that depends on jj-lib:
│                         #   RemoteBackend, RemoteOpStore, RemoteOpHeadsStore
└── jj-django/            # binary: CliRunner + StoreFactories ("django-jj"),
                          #   plus `init`, `clone`, `worker`
```

Starting point: jj's `cli/examples/custom-backend` at tag `v0.45.1`. For the op store and
op-heads store, `lib/src/simple_op_store.rs` and `lib/src/simple_op_heads_store.rs` are
the references.
