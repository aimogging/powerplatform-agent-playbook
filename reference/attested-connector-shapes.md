# Attested connector action shapes

"Attested" = copied from action definitions that imported and ran on a real tenant (values replaced with
placeholders). Use these shapes verbatim and change only values. Anything not on this page: get it from the
designer's "Peek code" in the target tenant, or from a probe flow plus the connector swagger
(`GET https://api.powerapps.com/providers/Microsoft.PowerApps/apis/shared_x?api-version=2016-11-01&$expand=properties/swagger&$filter=environment eq '<ENV_ID>'`)
-- never from memory.

Every OpenApiConnection action: `inputs.host = {apiId, connectionName, operationId}` where `connectionName` is the
KEY in `properties.connectionReferences` (e.g. `shared_sharepointonline`). Never add `inputs.authentication` (the
legacy importer refuses it). Add a `retryPolicy` to every connector action. Definition-level `parameters` carry
`$connections` (Object) and `$authentication` (SecureObject).

## connectionReferences entry (package source form)

```json
"shared_sharepointonline": {
  "connectionName": "shared-sharepointonline-<FlowName>",
  "source": "Embedded",
  "id": "/providers/Microsoft.PowerApps/apis/shared_sharepointonline",
  "tier": "NotSpecified",
  "apiName": "sharepointonline",
  "isProcessSimpleApiReferenceConversionAlreadyDone": false
}
```

The `connectionName` value is re-bound at import (the dialog asks for a connection per slot). A real, tenant-minted
name (32 hex chars, or `shared-<api>-<GUID>`) is needed only for API deploys.

## PowerApp trigger (one JSON text input) and Respond

```json
"triggers": { "manual": { "type": "Request", "kind": "PowerApp", "inputs": { "schema": {
  "type": "object", "required": ["payload"],
  "properties": { "payload": { "title": "payload", "type": "string", "x-ms-content-hint": "TEXT",
                               "x-ms-dynamically-added": true, "description": "JSON text: {...}" } } } } } }
```

```json
"Respond_to_PowerApps": { "type": "Response", "kind": "PowerApp", "runAfter": { "Compose_Result": ["Succeeded"] },
  "inputs": { "statusCode": 200, "body": { "result_json": "@string(outputs('Compose_Result'))" },
    "schema": { "type": "object", "properties": {
      "result_json": { "title": "result_json", "x-ms-dynamically-added": true, "type": "string" } } } } }
```

Caller identity inside an app-called flow (platform-stamped headers, never a caller-supplied field):
`@toLower(trim(coalesce(triggerOutputs()?['headers']?['x-ms-user-email'], triggerOutputs()?['headers']?['x-ms-client-principal-name'], '')))`

## SharePoint (`shared_sharepointonline`)

| operationId | parameters |
|---|---|
| `GetItems` | `dataset` (site URL), `table` (list GUID or title), `$filter` (OData v3), optional `$top`; `runtimeConfiguration.paginationPolicy.minimumItemCount` for more than one page |
| `GetItem` | `dataset`, `table`, `id` |
| `PostItem` | `dataset`, `table`, `item/<InternalName>` per column; Choice `item/<Col>/Value`; Lookup `item/<Col>/Id` |
| `PatchItem` | as PostItem + `id`; include `item/Title` if Title is required |
| `DeleteItem` | `dataset`, `table`, `id` |
| `GetOnUpdatedItems` (trigger) | `dataset`, `table`; trigger-level `recurrence`, `splitOn: @triggerOutputs()?['body/value']`, `conditions: [{expression: "@equals(triggerBody()?['Status']?['Value'],'New')"}]` |
| `CreateFile` | `dataset`, `folderPath` (LIBRARY-relative, e.g. `/Shared Documents/x`), `name`, `body` (binary, e.g. `@base64ToBinary(...)`) |
| `GetFileContent` | `dataset`, `id`, `inferContentType` |
| `HttpRequest` ("Send an HTTP request to SharePoint") | `dataset`, `parameters/method`, `parameters/uri` (`_api/...`, relative), `parameters/headers`, `parameters/body` (object); the connector adds the request digest |

Also attested (use Peek code for their exact parameters): `CreateAttachment`, `CreateNewFolder`, `ExtractFolderV2`,
`GetFileContentByPath`, `GetFileMetadataByPath`, `GetFolderMetadataByPath`, `ListFolder`.

Examples:

```json
"Create_Ticket": { "type": "OpenApiConnection", "runAfter": {},
  "inputs": { "host": { "apiId": "/providers/Microsoft.PowerApps/apis/shared_sharepointonline",
                        "connectionName": "shared_sharepointonline", "operationId": "PostItem" },
    "parameters": { "dataset": "https://contoso.sharepoint.com/sites/HelpDesk", "table": "ContosoHelpDeskTickets",
                    "item/Title": "@outputs('Compose_Subject')", "item/Status/Value": "New" },
    "retryPolicy": { "type": "exponential", "count": 3, "interval": "PT10S", "minimumInterval": "PT5S", "maximumInterval": "PT1M" } } }
```

```json
"Get_Fields": { "type": "OpenApiConnection", "runAfter": {},
  "inputs": { "host": { "apiId": "/providers/Microsoft.PowerApps/apis/shared_sharepointonline",
                        "connectionName": "shared_sharepointonline", "operationId": "HttpRequest" },
    "parameters": { "dataset": "https://contoso.sharepoint.com/sites/HelpDesk", "parameters/method": "GET",
                    "parameters/uri": "_api/web/lists/GetByTitle('ContosoHelpDeskTickets')/fields?$select=InternalName,TypeAsString",
                    "parameters/headers": { "Accept": "application/json;odata=nometadata" } } } }
```

MERGE through HttpRequest: `parameters/method: POST` + headers `X-HTTP-Method: MERGE`, `IF-MATCH: *` (or the item's
etag for a claim), `Content-Type: application/json;odata=verbose`, body with `__metadata.type` read from
`ListItemEntityTypeFullName`. Server-side file operations without moving bytes through the flow:
`_api/SP.MoveCopyUtil.MoveFolder` / `CopyFile` with `{srcUrl, destUrl}` (absolute URLs).

## Office 365 Outlook (`shared_office365`)

| operationId | parameters |
|---|---|
| `SendEmailV2` | `emailMessage/To` (`;`-separated), `emailMessage/Subject`, `emailMessage/Body` (HTML), `emailMessage/Importance` |
| `GetEmailsV3` | optional `mailboxAddress`, `top` (max 1000), `includeAttachments` (true for contentBytes) |

Also attested: `GetEmailV2`, `OnNewEmailV3`, `OnFlaggedEmailV4`, `Flag_V2`.

## Approvals (`shared_approvals`)

```json
"Create_an_approval": { "type": "OpenApiConnection",
  "inputs": { "host": { "apiId": "/providers/Microsoft.PowerApps/apis/shared_approvals", "connectionName": "shared_approvals",
                        "operationId": "CreateAnApproval" },
    "parameters": { "approvalType": "CustomResponse",
      "ApprovalCreationInput/responseOptions": ["Approve", "Return for changes"],
      "ApprovalCreationInput/title": "@outputs('Compose_Title')",
      "ApprovalCreationInput/assignedTo": "@outputs('Compose_Approvers')",
      "ApprovalCreationInput/details": "@outputs('Compose_Details')",
      "ApprovalCreationInput/itemLink": "@outputs('Compose_Link')",
      "ApprovalCreationInput/itemLinkDescription": "Open the item",
      "ApprovalCreationInput/enableNotifications": false,
      "ApprovalCreationInput/enableReassignment": false } } },
"Wait_for_an_approval": { "type": "OpenApiConnectionWebhook", "runAfter": { "Create_an_approval": ["Succeeded"] },
  "inputs": { "host": { "apiId": "/providers/Microsoft.PowerApps/apis/shared_approvals", "connectionName": "shared_approvals",
                        "operationId": "WaitForAnApproval" },
    "parameters": { "approvalName": "@body('Create_an_approval')?['name']" } } }
```

`assignedTo` accepts an M365 group address. The `approvalName` expression above follows the create action's output;
verify the output field name in your tenant's run history (the attested flow composed the id in an intermediate step).

## Teams (`shared_teams`)

`PostMessageToConversation`: `poster` ("Flow bot"), `location` ("Chat with Flow bot" or "Channel"),
`body/recipient` (UPN for a chat; group/channel ids for a channel), `body/messageBody` (HTML). `AtMentionUser`: one
`userId`. Also attested: `PostCardToConversation`, `PostCardAndWaitForResponse`, `ReplyWithCardToConversation`,
`UpdateCardInConversation`, `ListTeamMembers`, tag operations (`GetTags`, `CreateTag`, `AddMemberToTag`,
`DeleteTagMember`, `GetTagMembers`, `AtMentionTag`).

## Office 365 Users, Groups, Groups Mail, OneDrive, Word

- `shared_office365users` `UserProfile_V2`: `id` (UPN), `$select`. `SearchUserV2` also attested.
- `shared_office365groups` `HttpRequestV2`: `Uri` (relative Graph v1.0 path), `Method` -- e.g. group members.
- `shared_office365groupsmail`: `OnNewEmailInGroup` (trigger), `GetConversationThread`, `GetThreadPost`, `GetAttachments`
  (bytes in `contentBytes`); see trap M-02 for limits.
- `shared_onedriveforbusiness`: `CreateFile`, `DeleteFile`, `ConvertFileByPath` (e.g. to PDF).
- `shared_wordonlinebusiness`: `GetFilePDF`.

## Built-in HTTP (not a connector)

```json
"Call_LLM": { "type": "Http", "runAfter": { "Compose_Request": ["Succeeded"] },
  "inputs": { "method": "POST", "uri": "https://<LLM_GATEWAY_HOST>/v1/chat/completions",
    "headers": { "Authorization": "@{concat('Bearer ', variables('ApiKey'))}", "Content-Type": "application/json" },
    "body": "@outputs('Compose_Request')",
    "retryPolicy": { "type": "none" } },
  "limit": { "timeout": "PT2M" } }
```

The built-in HTTP action in an app-called flow does not make the app premium; the "HTTP with Microsoft Entra ID"
connector (`shared_webcontents`) does.
