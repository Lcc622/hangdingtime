# DingTalk Handingtime Automation Plan

## Goal

Automate the daily handingtime update flow without manual upload, manual commands, or manual password entry.

Current DingTalk group behavior:

- Around 09:00 every day, a DingTalk group robot posts the daily Handling Time update plan.
- The group message includes CSV files such as:
  - `damaus_instock_ht2.csv`
  - `damaus_outofstock_ht4.csv`
  - `epus_instock_ht2.csv`
- The CSV filenames already contain enough information to infer the shop account and target handingtime value.

The desired behavior:

1. At 10:00 every day, the handingtime service checks whether today's update files exist.
2. It fetches the required CSV files automatically.
3. It infers shop account and handingtime days from filenames.
4. It executes all matching files sequentially.
5. After all files finish, it sends a DingTalk notification with the execution summary.
6. Eccang login account/password are supplied by service environment variables, not entered manually in the UI.

## Important DingTalk Constraint

Do not rely on this approach:

```text
Get DingTalk group ID -> read group history -> find today's 09:00 message -> download CSV attachments from the group chat
```

This is risky because DingTalk group robots are mainly designed for sending messages. The stable file-download API generally depends on the robot receiving a file message and getting a `downloadCode`; DingTalk group-chat robot scenarios can have limitations around receiving file messages. Therefore, directly pulling historical group attachments by group ID is not a reliable implementation path.

## Recommended Plan A: DingTalk Drive Transit

Use DingTalk Drive as the reliable source of files.

### Upstream Change

The robot/script that posts the 09:00 DingTalk group message should also upload the generated CSV files into a fixed DingTalk Drive directory, for example:

```text
DingTalk Drive/handingtime/YYYY-MM-DD/
```

Example:

```text
DingTalk Drive/handingtime/2026-05-06/damaus_instock_ht2.csv
DingTalk Drive/handingtime/2026-05-06/damaus_outofstock_ht4.csv
DingTalk Drive/handingtime/2026-05-06/epus_instock_ht2.csv
```

### Daily Flow

At 10:00:

1. The handingtime service calls DingTalk OpenAPI with an internal app credential.
2. It locates today's folder or filters files by modified time after 09:00.
3. It downloads matching CSV files.
4. It validates filenames against:

```text
<shop-prefix>_<stock-type>_ht<days>.csv
```

Examples:

```text
epus_instock_ht2.csv -> AmazonEPUS, handing_time=2
damaus_outofstock_ht4.csv -> Amazon_PZnew_US_US, handing_time=4
```

5. It creates a batch task containing all valid files.
6. It executes files sequentially using the existing handingtime update queue.
7. It sends a DingTalk notification after all files complete.

### Required Configuration

```text
ECCANG_USER=CNSZ401
ECCANG_PASS=<eccang-password>
DINGTALK_APP_KEY=<internal-app-key>
DINGTALK_APP_SECRET=<internal-app-secret>
DINGTALK_DRIVE_ROOT=<drive-folder-id-or-path>
DINGTALK_NOTIFY_WEBHOOK=<group-robot-webhook>
DINGTALK_NOTIFY_SECRET=<group-robot-signing-secret>
HT_AUTO_SCAN_ENABLED=1
HT_AUTO_SCAN_TIME=10:00
```

### Result Notification Example

```text
Handling Time execution completed | 2026-05-06

Files: 4
Completed: 4
Failed: 0

damaus_instock_ht2.csv
- account: Amazon_PZnew_US_US
- handing_time: 2
- success: 6200
- not_found: 12
- failed: 0

damaus_outofstock_ht4.csv
- account: Amazon_PZnew_US_US
- handing_time: 4
- success: 1239
- not_found: 3
- failed: 0
```

## Alternative Plan B: Upstream Direct Push

If the script/robot that generates the CSV files can be modified, this is the most stable and simplest option.

### Flow

1. The 09:00 script generates CSV files.
2. It posts the existing DingTalk group message.
3. It also sends the CSV files directly to the handingtime service through an internal API:

```text
POST /api/dingtalk/batches
```

4. The handingtime service stores the files, infers account/days from filenames, queues execution, and sends DingTalk notification after completion.

### Benefits

- No need to read group history.
- No need to download group attachments.
- No dependency on DingTalk Drive file-list permissions.
- Fewer API failure points.

### Required Configuration

```text
ECCANG_USER=CNSZ401
ECCANG_PASS=<eccang-password>
HT_INGEST_TOKEN=<shared-secret-token>
DINGTALK_NOTIFY_WEBHOOK=<group-robot-webhook>
DINGTALK_NOTIFY_SECRET=<group-robot-signing-secret>
```

## Implementation Recommendation

Prefer Plan B if the upstream CSV-generating robot/script can be changed.

If the upstream robot/script cannot be changed except for saving files somewhere, use Plan A with DingTalk Drive transit.

Avoid implementing a solution based on group ID plus group-message history attachment scraping unless DingTalk account permissions and APIs are verified to support it in this enterprise environment.

## Proposed Development Steps

1. Add persistent batch model:
   - one batch contains multiple source CSV files
   - batch status tracks queued/running/completed/failed
2. Add filename validation and account mapping reuse:
   - `EPUS -> AmazonEPUS`
   - `DAMAUS -> Amazon_PZnew_US_US`
   - `ht<number> -> handing_time`
3. Add automatic scheduler:
   - runs daily at `HT_AUTO_SCAN_TIME`
   - default `10:00`
4. Add DingTalk file provider:
   - Plan A: DingTalk Drive listing/download provider
   - Plan B: direct upload ingest API
5. Add DingTalk notification sender:
   - signed group robot webhook
   - success/failure summary
6. Move Eccang credentials fully to environment variables:
   - remove requirement for UI password input in automatic mode
7. Keep the current web UI as a manual fallback:
   - manual upload
   - status view
   - logs and CSV result download

