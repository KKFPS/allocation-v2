Now I have everything I need to write the spec. Here it is:

---

# Specification: `microlise_functions.py`

**Module purpose:** Acts as the integration layer between the FPS back-office allocation system and the Microlise Transport Management System (TMS). After the internal optimizer has produced a vehicle-to-route allocation, this module translates the FPS-internal vehicle IDs into Microlise vehicle IDs and pushes each assignment to the Microlise Journeys Web API. It also generates and uploads post-allocation Excel reports to Azure Blob Storage.

---

## Environment Variables / Configuration

| Variable | Purpose | Default |
|---|---|---|
| `JLP_Microlise_TokenClientId` | OAuth2 client ID for Microlise token endpoint | `''` |
| `JLP_Microlise_TokenClient_Secret` | OAuth2 client secret for Microlise token endpoint | `''` |
| `JLP_Microlise_Token_URL` | Microlise OAuth2 token endpoint URL | `''` |
| `JLP_Microlise_JourneysWebAPI_URL` | Base URL of the Microlise Journeys Web API | `''` |
| `simulate_response` | When `'True'`, skips real API calls and synthesises a 201 response | `'True'` |
| `start_allocation_period` | Comma-separated `H, M, S` tuple defining the start of the live-API window | `'True'` (overridden at runtime) |
| `end_allocation_period` | Comma-separated `H, M, S` tuple defining the end of the live-API window | `'True'` (overridden at runtime) |
| `storage_account_conn_string` | Azure Storage Account connection string for report upload | `''` |
| `allocation_blob_container` | Azure Blob Storage container name | `''` |
| `allocation_blob_dir` | Folder prefix within the container for report blobs | `''` |
| `send_report` | When `'True'`, enables report generation and upload | `'False'` |

---

## External API Interactions

### 1. Microlise OAuth2 Token Endpoint

**Trigger:** Called once per `main()` execution when `simulate_response == 'False'`.

**Mechanism:** HTTP POST using HTTP Basic Auth (`client_id`:`client_secret`) with a `application/x-www-form-urlencoded` body.

**Request:**
- **Method:** `POST`
- **URL:** `JLP_Microlise_Token_URL`
- **Auth:** HTTP Basic Auth — `(client_id, client_secret)`
- **Body fields:** `grant_type = client_credentials`, `scope = journeyallocatevehicle`
- **Redirects:** disabled (`allow_redirects=False`)

**Response:** JSON object; the field `access_token` is extracted and used as a Bearer token for all subsequent Journeys API calls.

**Error handling:** All `requests` exception types (`HTTPError`, `ConnectionError`, `Timeout`, `TooManyRedirects`, `RequestException`) are caught, logged, and re-raised, aborting `main()`.

---

### 2. Microlise Journeys Web API — Vehicle Allocation

**Trigger:** Called once per route row in the allocation batch, only when `simulate_response == 'False'`.

**Mechanism:** HTTP POST to the Journeys API, one call per vehicle-to-route assignment.

**Request:**
- **Method:** `POST`
- **URL:** `{JLP_Microlise_JourneysWebAPI_URL}{route_id}/vehicles/`
- **Headers:** `content-type: text/json`, `Authorization: Bearer {token}`
- **Body (JSON):**
  ```json
  {
    "VehicleName": "<Microlise vehicle label>",
    "scope": "journeyallocatevehicle"
  }
  ```
  The `VehicleName` is the Microlise `telematic_label` for the vehicle, resolved from the `t_vehicle_telematics` table (see database section).

**Response handling (`http_response_handler`):**

| Status code | Classification | Action |
|---|---|---|
| `200` | Success (already allocated) | Returns `True`; no alert |
| `201` | Success (new allocation) | Returns `True`; no alert |
| `400` | Expected failure (bad request) | Inserts row into `t_alert` (message ID `9`); returns `False` |
| `401` | Expected failure (unauthorised) | Inserts row into `t_alert` (message ID `9`); returns `False` |
| `403` | Expected failure (forbidden) | Inserts row into `t_alert` (message ID `9`); returns `False` |
| `404` | Expected failure (resource not found) | Inserts row into `t_alert` (message ID `9`); returns `False` |
| Any other | Unexpected failure | Inserts row into `t_alert` (message ID `9`); returns `False` |

The `dev_app_id` field written to `t_alert` carries a human-readable string: `"Vehicle allocation: Microlise server response - {status_code} {response_text}"`. When `connection_type == 'test'` the suffix `": TEST"` is appended.

**Simulation mode:** When `simulate_response == 'True'`, a synthetic `requests.models.Response` object with `status_code = 201` is returned without making any network call. This is the default outside the configured live-API time window, or when `dynamic_allocation` is disabled.

---

### 3. Azure Blob Storage — Report Upload

**Trigger:** At the end of `main()`, conditional on all three of: `SEND_REPORT == 'True'`, `params.trigger_type == 'initial'`, and `simulate_response == 'False'`, and at least one of `params.initial_report` or `params.compliance_report` being `True`.

**Mechanism:** The report is assembled as an in-memory `BytesIO` Excel workbook using `openpyxl`. It is uploaded via `azure-storage-blob` `BlobClient`.

**Blob path:** `{allocation_blob_dir}/{report_file_name}.xlsx` where `report_file_name` is `daily_allocation_report_{site_id}_{YYYY_MM_DD}`.

**Connection:** `BlobClient.from_connection_string(CONNECTION_STRING, CONTAINER_NAME, blob_name)`, `overwrite=True`, `ContentSettings(content_type='file/xlsx')`.

**Error handling:** Any exception during upload is caught and logged as an error, but does not abort the calling function.

---

## Database Interactions

The module connects to a **PostgreSQL** back-office database via `psycopg2`. There are two connection types: `'test'` and `'prod'`, selected via the `connection_type` argument passed through `main()`. Connection credentials are resolved from environment variables.

### Tables Read

#### `t_allocation_monitor`

**Purpose:** Retrieve the `site_id` and metadata for the current allocation run, and — during compliance report generation — determine whether a qualifying initial allocation took place on the previous day.

**Query in `main()`:**
```sql
SELECT * FROM t_allocation_monitor WHERE allocation_id = <allocation_id>
```
Returns one row. The `site_id` column is extracted to scope all subsequent route queries to the correct client site.

**Query in `generate_compliance_report()`:**
```sql
SELECT * FROM t_allocation_monitor
WHERE CAST(run_datetime AS DATE) = <report_date>
```
Returns all allocation monitor rows for the previous day. The result is filtered in-memory to rows where `trigger_type = 'initial'`. If exactly one initial row exists whose `run_datetime` hour falls before `params.start_hour_allocation` and at or after `params.end_hour_allocation`, its `allocation_id` is used to pull the corresponding history.

---

#### `t_route_allocated`

**Purpose:** The set of routes that have been assigned vehicles by the optimizer and are pending API dispatch.

**Query in `main()`:**
```sql
SELECT * FROM t_route_allocated WHERE status = 'N'
```
Results are filtered in-memory to the current `site_id`. Rows where `http_response` is already `200`, `201`, or `-1` (i.e., previously dispatched or not requiring dispatch) are excluded before iterating.

**Columns used:** `vehicle_id_allocated`, `route_id`, `site_id`, `http_response`.

---

#### `t_route_allocated_history`

**Purpose:** Historical record of vehicle-route assignments used by the compliance report.

**Query in `generate_compliance_report()`:**
```sql
SELECT * FROM t_route_allocated_history WHERE allocation_id IN <tuple of route_ids>
```
Used only when a qualifying initial allocation is found. The result is merged with `t_route_plan` on `route_id` to compare planned vs. allocated vehicles.

---

#### `t_route_plan`

**Purpose:** The official route plan (vehicle IDs actually used) for the previous day, used as ground truth in the compliance report.

**Query in `generate_compliance_report()`:**
```sql
SELECT * FROM t_route_plan WHERE route_id IN <tuple of route_ids>
```
Merged with `t_route_allocated_history` on `route_id`. The `vehicle_id_y` column (from `t_route_plan`) is compared against `vehicle_id_allocated` (from history, mapped to Microlise IDs) to produce the `Vehicle Match` boolean column.

---

#### `t_vehicle_telematics`

**Purpose:** Bidirectional lookup table mapping FPS internal vehicle IDs to Microlise `telematic_label` values (i.e., the vehicle names the Microlise API recognises).

**Query (via `dbh.microlise_vehicle_dict()`):**
```sql
SELECT vehicle_id, telematic_label
FROM t_vehicle_telematics
WHERE telematic_id = 2
```
Result is loaded into two dictionaries: `fps_id → microlise_label` and `microlise_label → fps_id`. The sentinel values `0 → 'X'` and `-1 → '-1'` are added programmatically. The forward dictionary is used to translate each allocated vehicle ID before the API call and before report generation.

---

#### `t_error_log`

**Purpose:** Source of route-fetch failure records used to identify routes that could not be retrieved from Microlise and were therefore absent from the allocation.

**Query in `find_missing_routes()`:**
```sql
SELECT * FROM t_error_log
WHERE error_datetime >= <today_local>
  AND error_datetime < <today_local + 1 day>
```
Results are filtered in-memory to rows where `module_no` contains `'microlise-route-fetch'` and `error_message` contains `'Issues with TMC API route alias call for route num '`. The route number is parsed out of the error message string. Any such route numbers not present in the current allocation's route aliases are reported as unallocated.

---

### Tables Written

#### `t_route_allocated` — `http_response` column

**Trigger:** After each individual Microlise API call (or simulated call).

**Query:**
```sql
UPDATE t_route_allocated
SET http_response = <status_code>
WHERE route_id = '<route_id>'
```
Records the HTTP response code returned by the Microlise Journeys API (or `201` in simulation). This persists per-route dispatch status so that already-dispatched routes are skipped on subsequent runs.

---

#### `t_allocation_monitor` — `status` column

**Trigger:** Once per `main()` execution, after all route iterations complete.

**Query:**
```sql
UPDATE t_allocation_monitor
SET status = '<S or F>'
WHERE allocation_id = <allocation_id>
```
Set to `'S'` if all API calls returned success codes, `'F'` if any failed.

---

#### `t_alert`

**Trigger:** On any Microlise API response outside of `{200, 201}`.

**Query (via `dbh.generate_database_alert()`):**
```sql
INSERT INTO t_alert
  (alert_flag, alert_action, alert_id, alert_date_time,
   action_required, dev_app_id, site_id)
VALUES
  (True, 'email', 9, now(), True,
   'Vehicle allocation: Microlise server response - <code> <body>[: TEST]',
   <site_id>)
```
`alert_id = 9` is the fixed message ID for Microlise API call failures. The `dev_app_id` field carries the full human-readable failure context.

---

## Report Generation Logic (`save_allocation_report` / `generate_compliance_report` / `find_missing_routes`)

Three optional report sheets are assembled into a single Excel workbook, each controlled by flags on the `AllocationParameters` object:

| Sheet name | Flag | Content |
|---|---|---|
| `Morning Report YYYY_MM_DD` | `params.initial_report` | Columns: Route ID, Vehicle ID (Microlise label), Route Number. One row per allocated route for today. |
| `Compliance Report YYYY_MM_DD` | `params.compliance_report` and `allocation_occurred == True` | Columns: Route ID, Route Number, Vehicle ID Allocated, Vehicle ID Used, Vehicle Match. Only rows where `Vehicle Match == False` and `route_status` is not `'X'` or `'E'`. |
| `Unallocated Report YYYY_MM_DD` | `params.unallocated_report` and `errors_found == True` | Columns: Unallocated Route (route aliases that had fetch errors and were not in the final allocation). |

The workbook is serialised to an in-memory `BytesIO` buffer and uploaded to Azure Blob Storage. Report generation only runs when `SEND_REPORT == 'True'`, the trigger is `'initial'`, and `simulate_response == 'False'`.