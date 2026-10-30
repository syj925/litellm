# Editing a channel connection

Open Channels and choose Edit. The Upstream connection section loads a safe summary of the current connection. Existing secrets and key fragments are never returned to the editor

Choose Change URL to replace the upstream API base. Use the exact base expected by that integration, including its required path. Paths such as `/v1` are preserved rather than added or removed automatically. An empty URL keeps each model's existing value

Enter a replacement in New API key to change the key. Empty or whitespace-only input keeps existing keys. Updating only the URL never changes a key, and updating only a key never changes a URL. Nothing is written until Save channel is selected. Cancel and Discard connection changes remove the local draft

The displayed model count describes the save scope. A connection patch applies to every deployment selected in that save, including newly selected members. Removed members keep their previous connection. Other channels are not updated. Mixed existing API bases remain distinct when only a new key is supplied

## API and consistency

`GET /retail/admin/channels/{id}/connection` returns a redacted summary, edit capability and channel version. The existing `PUT /retail/admin/channels/{id}` accepts an optional `connection_patch` containing `api_base` and/or `api_key`. Both operations require a writable proxy administrator. The existing expected-version check prevents stale channel edits

The channel metadata, pool bindings, members and encrypted native model fields are saved in one PostgreSQL transaction. Only `api_base` and `api_key` are replaced in the encrypted model parameters. Model identities, mappings, prices, other provider options and unchanged binding configuration retain their existing behavior

The runtime reconciles the written model configuration and verifies the resulting live connection fields before reporting success. A refresh or notification failure triggers compensation and another runtime reconcile. Compensation checks the channel version, written model rows and affected bindings before restoring, so it cannot overwrite a concurrent operator's later edits. If those checks fail, the operation returns a safe error requiring administrator review. Restored channel versions continue increasing, and interrupted post-commit work also attempts compensation

## Supported storage and authentication

Direct API-key connections in native database-backed models are supported. A transient database-lock challenge verifies that retail and native model storage share the same database before allowing a combined write

Static model definitions, shared named LLM Credentials, and integrations using a different explicit authentication mechanism without an API key remain read-only in this editor. The summary explains the reason; normal channel organization remains available. Changing a shared credential here would affect other consumers, so this endpoint does not modify shared credential objects or silently detach them

Previously stored URLs containing credentials, query parameters, fragments or unresolved environment references are not returned as editable URL values. New bases require HTTP or HTTPS and reject embedded credentials, query strings, fragments and control characters. New keys are encrypted with the native model encryption helper and are not included in response or validation-error bodies
