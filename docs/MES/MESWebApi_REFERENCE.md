# MESWebApi — Endpoint Reference

Auto-generated from `MESWebApi.pdf`. **157 endpoints** across 21 controllers.

## Connection

- **Base URL:** `https://mypenm0soap03.corp.jabil.org/meswebapi/`
- **Endpoint pattern:** `POST https://mypenm0soap03.corp.jabil.org/meswebapi/<Controller>/<Method>`
  - e.g. `POST https://mypenm0soap03.corp.jabil.org/meswebapi/Customer/ListCustomer`
- **Auth:** send header `APIKey: <your key>` on **every** call.
- **Method:** all endpoints are **POST**; params go in a JSON body (`[FromBody]` DTO).
- **`sqlServer` / `dataBase`:** omitted — sending them (even blank) makes the API try to connect to an empty SQL host and returns a SqlException. Leave them out; the API uses its default DB.
- **Writes:** any ⚠️ endpoint also needs a valid `usrId` (a configured MES user).

> Start with a read like `Customer/ListCustomer` to confirm the key + host work, then expand.

## Contents

- **Assembly** (12)
- **Batch** (15)
- **Bom** (2)
- **Container** (3)
- **Customer** (3)
- **DB** (1)
- **Equipment** (6)
- **EquipmentSetup** (22)
- **Label** (5)
- **Link** (4)
- **Material** (12)
- **PV** (1)
- **Panel** (7)
- **Reporting** (2)
- **Route** (7)
- **RouteStepSetup** (7)
- **Security** (3)
- **Serial** (2)
- **Site** (1)
- **Test** (15)
- **Wip** (27)


---

## Assembly

### 1. GetAssemblyById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/GetAssemblyById`

**Body params:**

- `assemId` — assembly_id
- `langId` — “0” is not localized or English

**Sample body:**

```json
{
  "assemId": "",
  "langId": "0"
}
```

### 2. GetAssemblyProgressionByAssemblyRouteStep

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/GetAssemblyProgressionByAssemblyRouteStep`

**Body params:**

- `assemId` — required
- `routeStepId` — required
- `custId` — “0” if not specific
- `langId` — “0” is not localized or English

**Returns:** FromAssembly_ID, FromNumber, FromRevision, FromVersion, FromAssembly, FromCustomer_ID, FromCustomer, FromBOM_ID, FromBOM, ToAssembly_ID, ToNumber, ToRevision, ToVersion, ToAssembly, ToCustomer_ID, ToCustomer, ToBOM_ID, ToBOM, RouteStep_ID, FactoryMARoute_ID, FactoryText, MAText, RouteText, StepText, DescrText, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "assemId": "",
  "routeStepId": "",
  "custId": "0",
  "langId": "0"
}
```

### 3. GetAssemblyProgressionRecursive

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/GetAssemblyProgressionRecursive`

**Body params:**

- `assemId`
- `custId` — required and validated

**Returns:** FromAssembly_ID, FromNumber, FromRevision, FromVersion, ToAssembly_ID, ToNumber, ToRevision, ToVersion, AssemblyLevel

**Notes:**

- If assemId is invalid, raise error with proper message
- If assemId doesn’t belong to custId, raise error with proper message
- If assmId is not active, raise error with proper message
- Only active assemblies (both from and to) belong to the same custId will be return
- If no assembly progression is found, return 0 row.
- First progression from input assembly_id is at level 0; the assembly level can
- increment, or decrement based on direction of assembly progression

**Sample body:**

```json
{
  "assemId": "",
  "custId": "0"
}
```

### 4. GetParentAssemblyRecursive

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/GetParentAssemblyRecursive`

**Body params:**

- `assemId`
- `custId` — required and validated

**Returns:** Assembly_ID, Number, Revision, Version, RequiredQuantity, ParentAssembly_ID, ParentNumber, ParentRevision, ParentVersion, AssemblyLevel

**Notes:**

- If assemId is invalid, raise error with proper message
- If assemId doesn’t belong to custId, raise error with proper message
- If assmId is not active, raise error with proper message
- Only active parent assembly belong to the same custId will be return
- If no parent is found, return 0 row.
- Input assembly_id is at level 0; this level must be increased by one for each next
- parent

**Sample body:**

```json
{
  "assemId": "",
  "custId": "0"
}
```

### 5. ListAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssembly`

**Body params:**

- `custId` — required and validated
- `active` — “1” is active
- `partialKey` — "" is not specific
- `langId` — “0” is not localized or English

**Returns:** Assembly_ID, AssemblyName, Number, Revision, Version, Descr, Description, Phantom, BOM_ID, Material, EffectiveFrom, EffectiveTo, Active, Customer_ID, CustomerText, Panel_ID, PanelName, Family_ID, FamilyName, BarcodeMask_ID, BarcodeMaskText, UseMultiPartBarCode, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "custId": "0",
  "active": "1",
  "partialKey": "",
  "langId": "0"
}
```

### 6. ListAssemblyBarcodeMask

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyBarcodeMask`

**Body params:**

- `assemId`
- `partialKey` — "" is not specific
- `langId` — “0” is not localized or English

**Returns:** BarcodeMask_ID, BarcodeMaskText, Mask

**Sample body:**

```json
{
  "assemId": "",
  "partialKey": "",
  "langId": "0"
}
```

### 7. ListAssemblyDataByCustomer

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyDataByCustomer`

**Body params:**

- `customerId` — string[], e.g., [2,3]
- `assemblyStatus` — e.g., “Active”
- `updatedAfter` — e.g., “2019-07-01”
- `langId` — “0” is not localized or English

**Returns:** CustomerId, CustomerName, DivisionName, AssemblyNumber, AssemblyRevision, ParentAssemblyNumber, ParentAssemblyRevision, Quantity, AssemblyFamily, AssemblyDescription, AssemblyStatus, UpdatedDate

**Sample body:**

```json
{
  "customerId": "0",
  "assemblyStatus": "",
  "updatedAfter": "2025-01-01 00:00:00",
  "langId": "0"
}
```

### 8. ListAssemblyMaterialByAssemblyAndDeviation

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyMaterialByAssemblyAndDeviation`

**Body params:**

- `AssemblyNumber`
- `AssemblyRevision`
- `AssemblyVersion`
- `DeviationNumber` — optional, if blank, “none” will be used
- `langId` — optional, if blank 0 will be used

**Returns:** AssemblyMaterial_ID, Assembly_ID, Number, Revision, Version, BuildStep, RequiredQuantity, PackoutQuantity, ScanPart, ScanRev, Dynamic, Serializd, Verifiable, Deviation_ID, DeviationNumber, LinkMaterial_ID, LinkObject_ID, LinkObject, LinkObjectRevision, UserID_ID, CheckDate, Alternates, LinkMask, RouteStep_ID, FactoryText, MAText, RouteText, StepText

**Sample body:**

```json
{
  "AssemblyNumber": "",
  "AssemblyRevision": "",
  "AssemblyVersion": "",
  "DeviationNumber": "",
  "langId": "0"
}
```

### 9. ListAssemblyPropertyByAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyPropertyByAssembly`

**Body params:**

- `usrId` — required and validated
- `number` — assembly number
- `revision` — assembly revision
- `langId` — “0” is not localized or English

**Returns:** Assembly_ID, Number, Revision, Version, Assembly, Descr, Description, Phantom, BOM_ID, EffectiveFrom, EffectiveTo, Active, Customer_ID, Customer, Division, Panel_ID, PanelType, Family_ID, Family, BarcodeMask_ID, Barcode, UseMultiPartBarCode, Property_ID, Property, PropertyValue

**Sample body:**

```json
{
  "usrId": "",
  "number": "",
  "revision": "",
  "langId": "0"
}
```

### 10. ListAssemblyPropertyByAssemblyId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyPropertyByAssemblyId`

**Body params:**

- `usrId` — required and validated
- `assemId`
- `partialKey` — required, it is used for SQL like. If you didn’t pass in exact
- `langId` — “0” is not localized or English

**Sample body:**

```json
{
  "usrId": "",
  "assemId": "",
  "partialKey": "",
  "langId": "0"
}
```

### 11. ListAssemblyRouteByAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyRouteByAssembly`

**Body params:**

- `assemId`
- `fmaRoute`
- `langId` — “0” is not localized or English

**Returns:** Assembly_ID, AssemblyName, Number, Revision, Version, Customer_ID, FactoryMARoute_ID, FactoryName, ManufacturingAreaName, RouteName, StartDate, FinishDate, EstimatedUnits, EstimatedTime, UserID_ID, LastUpdated, FactoryMA_ID, CheckDate

**Sample body:**

```json
{
  "assemId": "",
  "fmaRoute": "",
  "langId": "0"
}
```

### 12. ListAssemblyForMIITool

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Assembly/ListAssemblyForMIITool`

**Body params:**

- `usrId`
- `customerId` — required and validated
- `partialKey` — “” is not specific

**Returns:** Assembly_ID, Number, Revision, Version, Assembly, Phantom, BOM_ID, SAP_BOM, EffectiveFrom, EffectiveTo, Active, Customer_ID, Division, Panel_ID, Family_ID, Family, BarcodeMask_ID, UseMultiPartBarCode

**Notes:**

- Batch

**Sample body:**

```json
{
  "usrId": "",
  "customerId": "0",
  "partialKey": ""
}
```


---

## Batch

### 13. AddBatchPulling ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/AddBatchPulling`

**Body params:**

- `usrId` — required and validated
- `fromBatchId` — required. BatchID_ID where initial batch
- `toBatchId` — required. BatchID_ID of end batch
- `routeStepId`

**Returns:** success/error

**Notes:**

- Batch Pulling configuration is stored in dbo.WP_BatchPulling table, which is outside of
- standard MES.exe
- fromBatchId, toBatchId and routeStepId constitute a unique key

**Sample body:**

```json
{
  "usrId": "",
  "fromBatchId": "",
  "toBatchId": "",
  "routeStepId": ""
}
```

### 14. DeleteBatchPulling ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/DeleteBatchPulling`

**Body params:**

- `usrId` — required and validated
- `batchPullingId` — required. BatchPulling_ID in dbo.WP_BatchPulling table

**Returns:** success/error

**Notes:**

- only unused configuration (no record found in dbo.WP_WipBatchPullingHistory) can
- be deleted

**Sample body:**

```json
{
  "usrId": "",
  "batchPullingId": ""
}
```

### 15. GetBatchById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/GetBatchById`

**Body params:**

- `batchId` — batchID_ID

**Sample body:**

```json
{
  "batchId": ""
}
```

### 16. GetBatchByNumber

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/GetBatchByNumber`

**Body params:**

- `batch`

**Sample body:**

```json
{
  "batch": ""
}
```

### 17. ListBatchAssemblyQty

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchAssemblyQty`

**Body params:**

- `batchId` — batchID_ID
- `assemId` — “0” if to retrieve qty for all assemblies associated with the batch
- `langId` — “0” is not localized or English – optional

**Returns:** BatchID_ID, BatchID, Assembly, Number, Revision, Version, AssemblyText, Assembly_ID, EnforceQuantity, EnforceQuantityByAssembly, BatchExpQty, BatchActQty, ExpQty, ActQty, UserID_ID, LastUpdated

**Sample body:**

```json
{
  "batchId": "",
  "assemId": "",
  "langId": "0"
}
```

### 18. ListBatchByAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchByAssembly`

**Body params:**

- `custId` — required
- `assemId` — required
- `partialKey` — required
- `langId` — “0” is not localized or English – optional

**Returns:** BatchID_ID, BatchID, Descr, ExpStartDateTime, ExpEndDateTime, ExpQty, EnforceDates, ActStartDateTime, ActEndDateTime, ActQty, UserID_ID, BatchCustomer_ID, EnforceQuantity, EnforceQuantityByAssembly, AutoAssociateProgAsm, BatchCustomerText, Assembly_ID, Assembly, Number, Revision, Version, AssemblyDescr, Phantom, BOM, EffectiveFrom, EffectiveTo, Active, AssemblyCustomer_ID, Panel, Family, BarcodeMask_ID, UserMultiPartBarcode, AssemblyCustomerText

**Notes:**

- This function will return up-to 400 rows of data
- Please set the partial key as precise as possible

**Sample body:**

```json
{
  "custId": "0",
  "assemId": "",
  "partialKey": "",
  "langId": "0"
}
```

### 19. ListBatchByAssemblyAndExpEndTime

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchByAssemblyAndExpEndTime`

**Body params:**

- `custId` — required
- `assemId` — required
- `expEndTime` — required
- `langId` — “0” is not localized or English – optional

**Returns:** BatchID_ID, BatchID, Descr, ExpStartDateTime, ExpEndDateTime, ExpQty, EnforceDates, ActStartDateTime, ActEndDateTime, ActQty, UserID_ID, BatchCustomer_ID, EnforceQuantity, EnforceQuantityByAssembly, AutoAssociateProgAsm, BatchCustomerText, Assembly_ID, Assembly, Number, Revision, Version, AssemblyDescr, Phantom, BOM, EffectiveFrom, EffectiveTo, Active, AssemblyCustomer_ID, Panel, Family, BarcodeMask_ID, UserMultiPartBarcode, AssemblyCustomerText

**Sample body:**

```json
{
  "custId": "0",
  "assemId": "",
  "expEndTime": "2025-01-01 00:00:00",
  "langId": "0"
}
```

### 20. ListBatchByDate

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchByDate`

**Body params:**

- `userId` — required, must be valid user ID greater than 1
- `StartDate` — required
- `EndDate` — required
- `customerId` — required
- `Number` — optional, can be blank string
- `Revision` — optional, can be blank string
- `Version` — optional, can be blank string
- `MaxCount` — 0 for no limit
- `Language_ID` — “0” is not localized or English – optional

**Returns:** BatchID_ID, BatchID, Descr, ExpStartDateTime, ExpEndDateTime, ExpQty, ActStartDateTime, ActEndDateTime, ActQty, Customer_ID, Assembly_ID

**Sample body:**

```json
{
  "userId": "",
  "StartDate": "2025-01-01 00:00:00",
  "EndDate": "2025-01-01 00:00:00",
  "customerId": "0",
  "Number": "",
  "Revision": "",
  "Version": "",
  "MaxCount": "",
  "Language_ID": ""
}
```

### 21. ListBatchCountsByRouteStep

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchCountsByRouteStep`

**Body params:**

- `CustomerID` — required
- `StartDate` — required
- `EndDate` — required

**Returns:** BatchID, Assembly, RouteStep, Bay, SAP_BOM, StepOrder, ActualQty, FirstScanDTS, LastScanDTS

**Notes:**

- Bay = Manufacturing Area in MES
- Due to the complexity of the query, this API will only run for a maximum 24 hour period on
- production servers.
- If a greater than 24 hour period is selected, the result will default to an end date 24 hours
- from the start date.
- Reporting servers etc. are not affected.
- Please work with your application admin to make sure sure the web.config of api have
- the following setting:
- configuration setting, sqlReportingTimeout, is used to alter SQL command
- timeout for GetReport method
- Timeout in seconds.
- E.g. 300. Please note that 0 will not be accepted. In the case 0 is set,
- the api will assume default behavior of 30 secs timeout

**Sample body:**

```json
{
  "CustomerID": "0",
  "StartDate": "2025-01-01 00:00:00",
  "EndDate": "2025-01-01 00:00:00"
}
```

### 22. ListBatchForPullingByAssemblyId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchForPullingByAssemblyId`

**Body params:**

- `custId` — required
- `assemId` — optional
- `routeStepId` — optional
- `isActive` — optinal; if we only list batches of active BatchPulling configurations
- `maxCount` — optional
- `langId` — “0” is not localized or English – optional

**Returns:** Assembly_ID, Number, Revision, Version, Customer_ID, CustomerText, DivisionText, Family_ID, FamilyText, BarcodeMask_ID, UseMultiPartBarCode, CustomerNumber, CustomerRevision, BuildStatus_ID, BuildStatus, BatchPulling_ID, ToBatchID_ID, ToBatchID, ToBatchExpQty, ToBatchActQty, FromAssembly_ID, FromNumber, FromRevision, FromVersion, FromBatchID_ID, FromBatchID, RouteStep_ID, UserID_ID, LastUpdated, CheckDate

**Notes:**

- Pass in valid assembly_id and routestep_id for more efficient search

**Sample body:**

```json
{
  "custId": "0",
  "assemId": "",
  "routeStepId": "",
  "isActive": "1",
  "maxCount": "",
  "langId": "0"
}
```

### 23. ListBatchPulling

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListBatchPulling`

**Body params:**

- `fromBatch` — optional, initial BatchID
- `toBatch` — optional, end BatchID
- `status` — optional. Active, Hold, Canceled or Fulfilled; default is Active

**Returns:** BatchPulling_ID, FromBatch, FromBatchExpQty, FromBatchActQty, ToBatch, ToBatchExpQty, ToBatchActQty, MAText, RouteText, StepText, RouteStep_ID, DescrText, Active, Status

**Sample body:**

```json
{
  "fromBatch": "",
  "toBatch": "",
  "status": ""
}
```

### 24. ListWipBatchPullingHistory

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/ListWipBatchPullingHistory`

**Body params:**

- `wipId`
- `serial` — wipId or serial must be provided

**Returns:** Wip_ID, SerialNumber, WipBatchPullingHistory_ID, BatchPulling_ID, FromBatchID_ID, FromBatchID, FromAssembly_ID, FromAssembly = Number/Revision/Version, ToBatchID_ID, ToBatchID, ToAssembly_ID, ToAssembly = Number/Revision/Version, RouteStep_ID, RouteStepText = MAText/RouteText/StepText, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "wipId": "1144749",
  "serial": "E50806012101079262600135"
}
```

### 25. UpdateBatchPullingStatus ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/UpdateBatchPullingStatus`

**Body params:**

- `usrId` — required and validated
- `batchPullingId` — required
- `routeStepId` — required; validated
- `status` — Active, Hold or Canceled

**Returns:** success/error

**Notes:**

- api to inactivate (hold or cancel) or activate (active) a batch pulling configuration
- to update RouteStep_ID for a particular BatchPulling, provide new routeStepId
- to update BatchPulling status only, provide current routeStepId for validation

**Sample body:**

```json
{
  "usrId": "",
  "batchPullingId": "",
  "routeStepId": "",
  "status": ""
}
```

### 26. WipBatchPulling

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/WipBatchPulling`

**Body params:**

- `custId` — required and validated
- `usrId` — required and validated
- `wipId` — required
- `batchPullingId` — required; BatchPulling_ID selected
- `toBatchId` — requires; end BatchID_ID
- `toAssemblyId` — required; end Assembly_ID
- `routeStepId`

**Returns:** success/error

**Notes:**

- Assign Board to ToBatch
- ToBatch ActQty incremented by 1
- FromBatch’s ActQty remains the same to avoid aging inventory concern -- boards left
- in FromBatch for longer than intended
- When ToBatch’s ActQty = ExpQty, set the associated BatchPulling configuration with
- Status == Fulfilled and Active == 0
- Log Wip batch change history to dbo.WP_WipBatchHistory table
- Process verification, Wip Move, and Assembly Progression remain unchanged, and
- not included in this stored procedure.

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "wipId": "1144749",
  "batchPullingId": "",
  "toBatchId": "",
  "toAssemblyId": "",
  "routeStepId": ""
}
```

### 27. WipBatchPullingReversal

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Batch/WipBatchPullingReversal`

**Body params:**

- `custId` — required and validated
- `usrId` — required and validated
- `serial` — required; Wip serial number
- `routeStepId`
- `assemId` — if 0 reverse back to birthing BatchID_ID and Assembly_ID;

**Returns:** success/error

**Notes:**

- Assign Board to its original batch and assembly
- Current batch ActQty decremented by 1
- Orignal ActQty remains the same to avoid aging inventory concern – it was not
- decremented when batch pulling occurred
- Change batchpulling configuration to Active after batchpulling reversal if batch pulling
- is at “Fulfilled” status
- Log Wip batch change history to dbo.WP_WipBatchHistory table
- Bom

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "serial": "E50806012101079262600135",
  "routeStepId": "",
  "assemId": ""
}
```


---

## Bom

### 28. GetBOMMaterialsByBOM

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Bom/GetBOMMaterialsByBOM`

**Returns:** BOM_ID, BOMMaterial_ID, BOMMaterial, Material_ID, Material, Description, Qty, EffectiveFrom, EffectiveTo, BOMLevel, Assembly, BOMSortOrder, UserID_ID, Checkdate

**Sample body:**

```json
{}
```

### 29. ListBOM

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Bom/ListBOM`

**Returns:** BOM_ID, BOMMaterial_ID, Material, Descr, Validated, UserID_ID, CheckDate

**Notes:**

- Container

**Sample body:**

```json
{}
```


---

## Container

### 30. GetBoxContents

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Container/GetBoxContents`

**Returns:** Container _ID, ContainerNumber CustomerText, WIP_ID, SerialNumber, Assembly_, Number, Revision, Version, PackDate

**Sample body:**

```json
{}
```

### 31. GetBoxContentsWithProperties

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Container/GetBoxContentsWithProperties`

**Sample body:**

```json
{}
```

### 32. GetBoxDataByBoard

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Container/GetBoxDataByBoard`

**Sample body:**

```json
{}
```


---

## Customer

### 33. ListCustomer

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Customer/ListCustomer`

**Body params:**

- `partialKey` — required
- `active` — = "1" if active only
- `langId` — “0” is not localized or English – optional

**Returns:** Customer_ID, Customer, CustomerName, Division, DivisionName, CustomerDivisionName, SAPIdentifier, ForceValidGRN, ProhibitLastPart, RequireQuickscan, UserID_ID, LastUpdated, CheckDate, Active

**Sample body:**

```json
{
  "partialKey": "",
  "active": "1",
  "langId": "0"
}
```

### 34. ListCustomerByFilter

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Customer/ListCustomerByFilter`

**Body params:**

- `customerStatus` — e.g., “Active”
- `updatedAfter` — e.g., “2019-07-01”
- `langId` — “0” is not localized or English – optional

**Returns:** Customer_ID, CustomerName, CustomerStatus, DivisionName, LastUpdated

**Sample body:**

```json
{
  "customerStatus": "",
  "updatedAfter": "2025-01-01 00:00:00",
  "langId": "0"
}
```

### 35. ListCustomerConfig

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Customer/ListCustomerConfig`

**Body params:**

- `custId` — required and validated
- `usrId` — required and validated
- `partialKey` — required
- `langId` — “0” is not localized or English – optional

**Returns:** Customer_ID, Customer, CustomerText, Division, DivisionText, KeyDescription, KeyValue, UserID_ID

**Notes:**

- DB

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "partialKey": "",
  "langId": "0"
}
```


---

## DB

### 36. RestoreSNHistoryWByWipID ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/DB/RestoreSNHistoryWByWipID`

**Body params:**

- `wipSNs` — Wip serials sparated by “|”
- `usrId` — UserID_ID
- `mode` — optional; default is 0
- `custId` — optional

**Returns:** success/error

**Notes:**

- Mode:
- 0 = Quiet, ReturnCode only
- 1 = Interactive (Upgrade Manager),
- 2 = Insert into a WP_WipsNoPurge Table with 1,1,1, also mode 2 is executed by SQL
- Job and SP "AUS_RestorePendingSNHistoryWByWip"
- Equipment

**Sample body:**

```json
{
  "wipSNs": "",
  "usrId": "",
  "mode": "",
  "custId": "0"
}
```


---

## Equipment

### 37. GetEquipmentById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Equipment/GetEquipmentById`

**Body params:**

- `usrId`
- `equipId` — equipment_id
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "usrId": "",
  "equipId": "",
  "langId": "0"
}
```

### 38. GetEquipmentByName

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Equipment/GetEquipmentByName`

**Body params:**

- `commonName` — equipment common name
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "commonName": "",
  "langId": "0"
}
```

### 39. GetEquipmentSetupGRNQty

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Equipment/GetEquipmentSetupGRNQty`

**Body params:**

- `usrId` — required and validated
- `routeStepId` — RouteStep_ID
- `assemId` — Assembly_ID for the serial number
- `equipId` — Equipment_ID
- `equipSetupId` — EquipmentSetup_ID
- `grn` — "" if not specific

**Returns:** List of (GRN, Material, Qty)

**Notes:**

- Equipmentsetup must be completed in MES
- If grn = "", List of (GRN, Material, Qty). Else if grn is specified, should have only 1 row

**Sample body:**

```json
{
  "usrId": "",
  "routeStepId": "",
  "assemId": "",
  "equipId": "",
  "equipSetupId": "",
  "grn": ""
}
```

### 40. GetFeederTrayTrackByName

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Equipment/GetFeederTrayTrackByName`

**Body params:**

- `feeder`
- `tray`
- `track`
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "feeder": "",
  "tray": "",
  "track": "",
  "langId": "0"
}
```

### 41. GetTransportByName

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Equipment/GetTransportByName`

**Body params:**

- `tranport`
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "tranport": "",
  "langId": "0"
}
```

### 42. ListEquipmentByRouteStep

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Equipment/ListEquipmentByRouteStep`

**Body params:**

- `routeStepId` — required and validated
- `langId` — “0” is not localized or English – optional

**Returns:** RouteStep_ID, FactoryName, ManufacturingAreaName, RouteName, StepName, RouteStepDescription = p.Translation, Occurrence, Equipment_ID, Vendor, Model, CommonName, EquipmentRow, EquipmentColumn, ProcessingTime, UserID_ID, LastUpdated, CheckDate

**Notes:**

- EquipmentSetup

**Sample body:**

```json
{
  "routeStepId": "",
  "langId": "0"
}
```


---

## EquipmentSetup

### 43. ClearSetupTable ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ClearSetupTable`

**Body params:**

- `usrId` — required and validated
- `setupId` — equipment setup Id
- `equipId`
- `clearMachine`
- `table`
- `removeTransport`

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "setupId": "",
  "equipId": "",
  "clearMachine": "",
  "table": "",
  "removeTransport": ""
}
```

### 44. ClearSetupTable ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ClearSetupTable`

**Body params:**

- `EquipmentSetup_ID`

**Returns:** success/error

**Sample body:**

```json
{
  "EquipmentSetup_ID": ""
}
```

### 45. EquipmentComponentAddPartByLane ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/EquipmentComponentAddPartByLane`

**Body params:**

- `usrId` — required and validated
- `setupId` — equipmentsetup_ID
- `equipId`
- `feederTrayTrackId` — ,
- `grnId`
- `qty`
- `tranportId`
- `setupFeederType`
- `loadedFeederType`
- `table`
- `installed`
- `lane`

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "setupId": "",
  "equipId": "",
  "feederTrayTrackId": "",
  "grnId": "",
  "qty": "",
  "tranportId": "",
  "setupFeederType": "",
  "loadedFeederType": "",
  "table": "",
  "installed": "",
  "lane": ""
}
```

### 46. EquipmentComponentRemovePart ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/EquipmentComponentRemovePart`

**Body params:**

- `usrId` — required and validated
- `equipId`
- `feederTrayTrackId` — ,
- `grnId`
- `table`
- `removeTransport`
- `reelIsEmpty`

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "equipId": "",
  "feederTrayTrackId": "",
  "grnId": "",
  "table": "",
  "removeTransport": "",
  "reelIsEmpty": ""
}
```

### 47. GetActiveEquipmentSetupByAssemblyId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/GetActiveEquipmentSetupByAssemblyId`

**Body params:**

- `Assembly_ID`
- `MaxCount` — optional, 0 returns all records
- `Language_ID` — “0” is not localized or English – optional

**Returns:** EquipmentSetup_ID, RouteStep_ID, Equipment_ID, Assembly_ID, Number, Revision, Version, AssemblyText, FactoryText, MAText, RouteText, StepText, CommonName, Vendor, Model, BOM_ID, SetupNumber, SetupVersion, TotalComponents, TotalPins, Export, Active, RFDisplay, MachineName, ProgramName, LoadDateTime, Assembly, CommonEquipmentSetup_ID, CommonSetupName, UserID_ID, CheckDate

**Sample body:**

```json
{
  "Assembly_ID": "",
  "MaxCount": "",
  "Language_ID": ""
}
```

### 48. GetEquipmentSetupByEquipmentAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/GetEquipmentSetupByEquipmentAssembly`

**Body params:**

- `Equipment_ID`
- `Assembly_ID`

**Returns:** EquipmentSetup_ID, RFDisplay, SetupNumber, SetupVersion, MaxVersion, MachineName, ProgramName, ForceValidGRN, ProhibitLastPart

**Sample body:**

```json
{
  "Equipment_ID": "",
  "Assembly_ID": ""
}
```

### 49. GetEquipmentSetupById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/GetEquipmentSetupById`

**Body params:**

- `Equipment_ID`
- `Language_ID` — “0” is not localized or English – optional

**Returns:** EquipmentSetup_ID, RouteStep_ID, Equipment_ID, Assembly_ID, Number, Revision, Version, AssemblyText, FactoryText, MAText, RouteText, StepText, CommonName, Vendor, Model, BOM_ID, SetupNumber, SetupVersion, TotalComponents, TotalPins, Export, Active, RFDisplay, MachineName, ProgramName, LoadDateTime, Assembly, CommonEquipmentSetup_ID, CommonSetupName, UserID_ID, CheckDate

**Sample body:**

```json
{
  "Equipment_ID": "",
  "Language_ID": ""
}
```

### 50. GetNewSetups

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/GetNewSetups`

**Body params:**

- `MachineName`
- `MaxCount` — optional, 0 is all records

**Returns:** EquipmentSetup_ID, MachineName, ProgramName, LoadDateTime, Assembly, TotalComponents, UserID_ID, CheckDate

**Sample body:**

```json
{
  "MachineName": "",
  "MaxCount": ""
}
```

### 51. InsertEquipmentSetup ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/InsertEquipmentSetup`

**Body params:**

- `EquipmentSetup_ID` — required, must be ID already in CT_Setup table but not
- `RouteStep_ID`
- `Equipment_ID`
- `Assembly_ID`
- `SetupNumber`
- `TotalComponents`
- `TotalPins`
- `Export`
- `Active`
- `RFDisplay`
- `CommonEquipmentSetup_ID`
- `UserID_ID` — required, must be valid user

**Returns:** Success: Will return 1, Failure: Will return error message

**Sample body:**

```json
{
  "EquipmentSetup_ID": "",
  "RouteStep_ID": "",
  "Equipment_ID": "",
  "Assembly_ID": "",
  "SetupNumber": "",
  "TotalComponents": "",
  "TotalPins": "",
  "Export": "",
  "Active": "1",
  "RFDisplay": "",
  "CommonEquipmentSetup_ID": "",
  "UserID_ID": ""
}
```

### 52. InsertEquipmentSetupFeeder ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/InsertEquipmentSetupFeeder`

**Body params:**

- `EquipmentSetup_ID` — required, must be valid equipment setup ID
- `Feeder`
- `Tray`
- `Track`
- `FeederType`
- `Media`
- `Material_ID`
- `MaterialDescr`
- `CRDCount`
- `OutOfStock`
- `Deviation`
- `Change`
- `UserID_ID` — required, must be valid user

**Returns:** Success: Will return new Feeder_ID, Failure: Will return error message

**Sample body:**

```json
{
  "EquipmentSetup_ID": "",
  "Feeder": "",
  "Tray": "",
  "Track": "",
  "FeederType": "",
  "Media": "",
  "Material_ID": "",
  "MaterialDescr": "",
  "CRDCount": "",
  "OutOfStock": "",
  "Deviation": "",
  "Change": "",
  "UserID_ID": ""
}
```

### 53. InsertFeederCRD ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/InsertFeederCRD`

**Body params:**

- `Feeder_ID` — required, must be valid feeder ID
- `CRD`
- `UserID_ID` — required, must be valid user

**Returns:** Success: Will return 1, Failure: Will return error message

**Sample body:**

```json
{
  "Feeder_ID": "",
  "CRD": "",
  "UserID_ID": ""
}
```

### 54. InsertFeederCRDOffset ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/InsertFeederCRDOffset`

**Body params:**

- `Feeder_ID` — required, must be valid feeder ID
- `CRD`
- `Offset`
- `UserID_ID` — required, must be valid user

**Returns:** Success: Will return 1, Failure: Will return error message

**Sample body:**

```json
{
  "Feeder_ID": "",
  "CRD": "",
  "Offset": "",
  "UserID_ID": ""
}
```

### 55. InsertSetup ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/InsertSetup`

**Body params:**

- `MachineName`
- `ProgramName`
- `Assembly`
- `TotalComponents`
- `UserID_ID` — required, must be valid user

**Returns:** Success: Will return EquipmentSetup_ID, Failure: Will return error message

**Sample body:**

```json
{
  "MachineName": "",
  "ProgramName": "",
  "Assembly": "",
  "TotalComponents": "",
  "UserID_ID": ""
}
```

### 56. InsertSetupSheetComment ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/InsertSetupSheetComment`

**Body params:**

- `EquipmentSetup_ID`
- `KeyDate`
- `Comment`
- `UserID_ID` — required, must be valid user

**Returns:** Success: Will return 1, Failure: Will return error message

**Sample body:**

```json
{
  "EquipmentSetup_ID": "",
  "KeyDate": "2025-01-01 00:00:00",
  "Comment": "",
  "UserID_ID": ""
}
```

### 57. ListEquipmentTableComponentById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ListEquipmentTableComponentById`

**Body params:**

- `equipId`
- `table` — equipment table
- `feederTrayTrackId`
- `langId` — “0” is not localized or English – optional

**Returns:** Equipment_ID, EquipmentMaster_ID, Make, Vendor, Model, CommonName, EquipmentTable, FeederTrayTrack_ID, Slot, Tray, Track, GRN_ID, GRN, Material_ID, Material, Qty, TransPort_ID, Transport, UserID_ID, LastUpdated, CheckDateQty

**Sample body:**

```json
{
  "equipId": "",
  "table": "",
  "feederTrayTrackId": "",
  "langId": "0"
}
```

### 58. ListFeederByEquipmentSetup

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ListFeederByEquipmentSetup`

**Body params:**

- `setupId` — equipmentsetup_id
- `langId` — “0” is not localized or English – optional

**Returns:** Feeder_ID, EquipmentSetup_ID, CommonEquipmentSetup_ID, CommonSetupName, Equipment_ID, Vendor, Model, CommonName, MachineName, ProgramName, LoadDateTime, Assembly_ID, Number, Revision, Version, AssemblyText, FeederTrayTrack_ID, Feeder, Tray, Track, FeederType, Media, Material_ID, Material, Descr, CRDCount, OutOfStock, Deviation, Change, XFeeder, LastUpdated, CheckDate, EffectiveDateFrom, EffectiveDateTo

**Sample body:**

```json
{
  "setupId": "",
  "langId": "0"
}
```

### 59. ListGRNTransportAssociationWithinTime

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ListGRNTransportAssociationWithinTime`

**Body params:**

- `startTime` — required in datetime format
- `endTime` — required in datetime format
- `maxCount` — optional, default = 400

**Returns:** LoadTime, Transport, GRN, WindowsUserID

**Notes:**

- The time between startTime and endTime is limited to 12 hours or 720 minutes

**Sample body:**

```json
{
  "startTime": "2025-01-01 00:00:00",
  "endTime": "2025-01-01 00:00:00",
  "maxCount": ""
}
```

### 60. ListLoadedFeederByEquipment

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ListLoadedFeederByEquipment`

**Body params:**

- `equipId`
- `table` — “0” if not specific

**Returns:** Transport, Transport_ID, Slot, Tray, Track, EquipmentTable, Qty, GRN_ID, GRN

**Sample body:**

```json
{
  "equipId": "",
  "table": ""
}
```

### 61. ListSetupByEquipmentAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/ListSetupByEquipmentAssembly`

**Body params:**

- `equipId`
- `assemId`
- `langId` — “0” is not localized or English – optional

**Returns:** EquipmentSetup_ID, RFDisplay, SetupNumber, SetupVersion, MaxVersion, MachineName, ProgramName, ForceValidGRN, ProhibitLastPart

**Sample body:**

```json
{
  "equipId": "",
  "assemId": "",
  "langId": "0"
}
```

### 62. SetSetupTableActiveByLane ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/SetSetupTableActiveByLane`

**Body params:**

- `usrId` — required and validated
- `setupId`
- `equipId`
- `activeTable`
- `routeStepId`
- `lane` — “” if not specific
- `xOutId` — “0” if not specific

**Returns:** (success: + xoutId )/error

**Sample body:**

```json
{
  "usrId": "",
  "setupId": "",
  "equipId": "",
  "activeTable": "1",
  "routeStepId": "",
  "lane": "",
  "xOutId": ""
}
```

### 63. SetupValidation ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/SetupValidation`

**Body params:**

- `setupId` — equipmentsetup_id

**Returns:** Block, ComponentLocation, Tray, Track, RequiredPart, ActualPart, GRN – GRN++Number in sp, Action

**Sample body:**

```json
{
  "setupId": ""
}
```

### 64. UpdateEquipmentSetup ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/EquipmentSetup/UpdateEquipmentSetup`

**Body params:**

- `EquipmentSetup_ID` — required, must be valid equipment setup ID
- `RouteStep_ID`
- `Equipment_ID`
- `Assembly_ID`
- `SetupNumber`
- `SetupVersion`
- `TotalComponents`
- `TotalPins`
- `Export`
- `Active`
- `RFDisplay`
- `CommonEquipmentSetup_I`
- `UserID_ID` — required, must be valid user
- `CheckDate` — required, must be match time of last known update

**Returns:** Success: Will return a record with:, Result: Error code from stored procedure. 0 = success., CheckDate: New checkdate of record., Failure: Will return error message

**Notes:**

- Label

**Sample body:**

```json
{
  "EquipmentSetup_ID": "",
  "RouteStep_ID": "",
  "Equipment_ID": "",
  "Assembly_ID": "",
  "SetupNumber": "",
  "SetupVersion": "",
  "TotalComponents": "",
  "TotalPins": "",
  "Export": "",
  "Active": "1",
  "RFDisplay": "",
  "CommonEquipmentSetup_I": "",
  "UserID_ID": "",
  "CheckDate": "2025-01-01 00:00:00"
}
```


---

## Label

### 65. EvaluateLabelFields ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Label/EvaluateLabelFields`

**Body params:**

- `custId`
- `usrId`
- `lblType`
- `assemId`
- `fmaId`
- `batchId`
- `langId` — “0” is not localized or English – optional

**Returns:** FieldOrder, LabelFieldType_ID, LabelField, QuantityPerLabel, FieldValue

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "lblType": "",
  "assemId": "",
  "fmaId": "",
  "batchId": "",
  "langId": "0"
}
```

### 66. GetContainerLabelsByContainer

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Label/GetContainerLabelsByContainer`

**Body params:**

- `custId`
- `container`
- `status`
- `langId` — “0” is not localized or English – optional

**Returns:** ContainerCapacity, SerialNumber_ID, SequenceNumber, ContainerStatus_ID, LabelType_ID, LabelConfig_ID, AssemblySpecific, LabelPackage_ID, LabelGroup_ID, LabelType, Description, LabelQuantity, DuplicateQuantity, BatchID_ID

**Sample body:**

```json
{
  "custId": "0",
  "container": "",
  "status": "",
  "langId": "0"
}
```

### 67. ListLabelConfig

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Label/ListLabelConfig`

**Body params:**

- `custId`
- `lblType`
- `assemId`
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "custId": "0",
  "lblType": "",
  "assemId": "",
  "langId": "0"
}
```

### 68. PrintBoxLabel ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Label/PrintBoxLabel`

**Body params:**

- `custId`
- `usrId`
- `box`
- `family`
- `assem`
- `rev`
- `capacity`
- `boxCnt`
- `created`
- `status`
- `usrName`
- `station`
- `contRule`
- `assemId`
- `fmaId`
- `batchId`
- `lblPackageCode`
- `printer`
- `extraData` — json string, e.g.,
- `example`
- `langId` — “0” is not localized or English – optional

**Returns:** success/error

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "box": "",
  "family": "",
  "assem": "",
  "rev": "",
  "capacity": "",
  "boxCnt": "",
  "created": "",
  "status": "",
  "usrName": "",
  "station": "",
  "contRule": "",
  "assemId": "",
  "fmaId": "",
  "batchId": "",
  "lblPackageCode": "",
  "printer": "",
  "extraData": "",
  "example": "",
  "langId": "0"
}
```

### 69. PrintLabel ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Label/PrintLabel`

**Body params:**

- `custId`
- `usrId`
- `lblType`
- `lblConfigId`
- `qty`
- `dupQty`
- `exePath`
- `dataDropPath`
- `lblDefPath`
- `assemId`
- `fmaId`
- `batchId`
- `lblPackageCode`
- `printer`
- `lblDefFile`
- `extraData` — json string, e.g.,
- `example`
- `langId` — “0” is not localized or English – optional

**Returns:** success/error

**Notes:**

- json example
- {
- "custId":"52",
- "usrId":"17328",
- "lblType":"FULLBOX",
- “lblConfigId":"34309",
- “qty":"1",
- “dupQty":"0",
- “exePath":"\\\\GDLLOFT02\\llmwin32\\LABELS",
- “dataDropPath":"c:\\Temp",
- “lblDefPath":"\\\\GDLLOFT02\\llmwin32\\LABELS",
- “assemId":"87941",
- “fmaId":"0",
- “batchId":"0",
- “lblPackageCode":"1",
- “printer":"",
- “dataFile":"",
- “lblDefFile":"ciscoempaquearrow.lwl",
- “example":"0",
- “langId":"0",
- “extraData":@"{""SOFTWAREREV"":"""",""FIRMWAREREV"":"""",""SERIE1"":""JAD2303
- 0JTH"",""SERIE2"":""JAD23030JTH"",""BOXSERIALNUMBER"":""JMX854847"",""BOXCOUN
- T"":""2""}
- }
- Link

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "lblType": "",
  "lblConfigId": "",
  "qty": "",
  "dupQty": "",
  "exePath": "",
  "dataDropPath": "",
  "lblDefPath": "",
  "assemId": "",
  "fmaId": "",
  "batchId": "",
  "lblPackageCode": "",
  "printer": "",
  "lblDefFile": "",
  "extraData": "",
  "example": "",
  "langId": "0"
}
```


---

## Link

### 70. GetBoardLinkInfo

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Link/GetBoardLinkInfo`

**Body params:**

- `wipId`
- `linkObject`
- `LinkObjectRev`
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "wipId": "1144749",
  "linkObject": "",
  "LinkObjectRev": "",
  "langId": "0"
}
```

### 71. LinkByLinkStation ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Link/LinkByLinkStation`

**Body params:**

- `custId` — required and validated
- `serial` — required
- `assemId`
- `routeStepId`
- `equipId`
- `linkDataJson`
- `usrId`

**Returns:** success/error

**Notes:**

- linkDataJson definition:
- linkObject – linkObject configured on LinkStation
- rev – linkObjectRev configured on LinkStation
- linkData – linkData
- part – scanned part number if alternate material is used. In the case when
- part == linkObject, linkData is for primarty material
- partRev – part revision for alternate material. It is only used if linking
- alternate material (part != linkObject)
- example:
- [
- {
- "linkObject":"78-0000-01",
- "rev":"01",
- "linkData": "JMX1212121",
- "part":"",
- "partRev":""
- },
- {
- "linkObject":"75-2222-03",
- "rev":"03",
- "linkData":"JMX3232323",
- "part":"",
- "partRev":""
- }
- ]
- All or nothing linking for the entire link station
- Business rule implemented:
- wip must belong to the customer and active
- wip must of selected assembly or can be progress to selected assembly
- wip must pass pv check via wip move
- link data for all link objects configured on the route step are present
- if ScanRev is turned on for a particular link object, revision must be supplied
- and validated
- if unique, it must not be used in the system per material
- if datamasks are configured, a link data is subjected to datamask verification
- if alternate material (by supplying part different than linkobject in
- linkDataJson), alternate material will be verified
- if linking a child board, the child assembly must be validated
- if linking a child board, the child wip must belong to the customer and active
- if linking a child board, pv of child wip
- link nonunique component when applied
- link unique component when applied
- link child component when applied
- wip move for child component when applied
- wip move for parent wip when applied

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "assemId": "",
  "routeStepId": "",
  "equipId": "",
  "linkDataJson": "",
  "usrId": ""
}
```

### 72. LinkWithValidation ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Link/LinkWithValidation`

**Body params:**

- `custId` — required and validated
- `assemId` — assembly id of the board
- `deviationId` — board deviation id. Pass in “0” if no deviation
- `boardSerial`
- `linkObject`
- `linkObjectRev`
- `linkData`
- `usrId` — required and validated
- `routeStepId` — required
- `equipId` — required
- `part` — = "" – for future purpose, if scanned in part number is different than link
- `rev` — = "" – for future purpose, if scanned in rev is different than link object rev
- `bypassChildLinkStepConfig` — = "0" – optional, only applies to the case when child

**Returns:** success/error

**Notes:**

- Assembly must be configuration for Linking operation via LinkStation
- Linking operation:
- Wip-In: Before linking is called, wip must move into route step that is configured
- for linking first. This is done by calling Wip/BoardWipMove (with IOType_ID =1,
- OpStatus = 4)
- Client will perform the linking of all required materials by calling this
- LinkWithValidation() function
- Wip-Out: Upon completion of all required materials’ linking, call
- Wip/BoardWipMove (with IOType_ID =2, OpStatus = 4) to signal the
- completion of the linking
- Wip-Out: call Wip/BoardWipMove (with IOType_ID =2, OpStatus = 3) in the
- case of error to signal the aborting of the operation
- This is a combined function to do the following
- Validate board and its assembly id, as well as its availability for linking (Scrap,
- OnHold, Active, etc)
- Get link station material and related information, including ScanPart, ScanRev,
- Serialize, Unique, ChildAssembly, AlternateMaterials, and Datamasks based on
- the board serial, board assembly id, board deviation id, and route step
- Validate linkobject, linkobjectrev, and routestep to make the link object was
- configured and still needed on the route step
- Validate link data’s serial number, uniqueness, datamask if so configured.
- Wip move for the child board if applied
- Linking – record linkdata and board relationship in database.
- Child link
- Link unique serial
- Link non-unique serial

**Sample body:**

```json
{
  "custId": "0",
  "assemId": "",
  "deviationId": "",
  "boardSerial": "E50806012101079262600135",
  "linkObject": "",
  "linkObjectRev": "",
  "linkData": "",
  "usrId": "",
  "routeStepId": "",
  "equipId": "",
  "part": "",
  "rev": "",
  "bypassChildLinkStepConfig": ""
}
```

### 73. LinkWithValidationByText ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Link/LinkWithValidationByText`

**Body params:**

- `customer` — customer name; required
- `division` — required
- `assembly` — assembly number
- `revision` — assembly revision
- `deviationId` — board deviation id. Pass in “0” if no deviation
- `boardSerial`
- `linkObject`
- `linkObjectRev`
- `linkData`
- `usrId` — required and validated
- `factory` — required
- `ma` — manufactory area; required
- `route` — required
- `stepDescr` — route step descr; required
- `equipment`
- `part` — = "" – for future purpose, if scanned in part number is different than link
- `rev` — = "" – for future purpose, if scanned in rev is different than link object rev
- `bypassChildLinkStepConfig` — = "0" – optional, only applies to the case when child

**Returns:** success/error

**Notes:**

- Assembly must be configuration for Linking operation via LinkStation
- Linking operation:
- Wip-In: Before linking is called, wip must move into route step that is configured
- for linking first. This is done by calling Wip/BoardWipMove (with IOType_ID =1,
- OpStatus = 4)
- Client will perform the linking of all required materials by calling this
- LinkWithValidation() function
- Wip-Out: Upon completion of all required materials’ linking, call
- Wip/BoardWipMove (with IOType_ID =2, OpStatus = 4) to signal the
- completion of the linking
- Wip-Out: call Wip/BoardWipMove (with IOType_ID =2, OpStatus = 3) in the
- case of error to signal the aborting of the operation
- This is a combined function to do the following
- Validate board and its assembly id, as well as its availability for linking (Scrap,
- OnHold, Active, etc)
- Get link station material and related information, including ScanPart, ScanRev,
- Serialize, Unique, ChildAssembly, AlternateMaterials, and Datamasks based on
- the board serial, board assembly id, board deviation id, and route step
- Validate linkobject, linkobjectrev, and routestep to make the link object was
- configured and still needed on the route step
- Validate link data’s serial number, uniqueness, datamask if so configured.
- Wip move for the child board if applied
- Linking – record linkdata and board relationship in database.
- Child link
- Link unique serial
- Link non-unique serial
- Material

**Sample body:**

```json
{
  "customer": "",
  "division": "",
  "assembly": "",
  "revision": "",
  "deviationId": "",
  "boardSerial": "E50806012101079262600135",
  "linkObject": "",
  "linkObjectRev": "",
  "linkData": "",
  "usrId": "",
  "factory": "",
  "ma": "",
  "route": "",
  "stepDescr": "",
  "equipment": "",
  "part": "",
  "rev": "",
  "bypassChildLinkStepConfig": ""
}
```


---

## Material

### 74. BlockGRN ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/BlockGRN`

**Body params:**

- `grn` — required.
- `blockTypeId` — required.
- `blocked` — required;
- `assembly` — optional
- `revision` — optional
- `version` — optional
- `family` — optional
- `mfgArea` — optional
- `route` — optional
- `usrId` — required.

**Returns:** success/error

**Notes:**

- GRN must not be loaded in a setup sheet.
- Provide the corrects parameters depending on the BlockType_ID. For example, if
- BlockType_ID is 5, add in the parameters the name of family.

**Sample body:**

```json
{
  "grn": "",
  "blockTypeId": "",
  "blocked": "",
  "assembly": "",
  "revision": "",
  "version": "",
  "family": "",
  "mfgArea": "",
  "route": "",
  "usrId": ""
}
```

### 75. CheckGRNMSL

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/CheckGRNMSL`

**Body params:**

- `usrId`
- `grnId`

**Returns:** integer value of ElapsedExposureTime

**Sample body:**

```json
{
  "usrId": "",
  "grnId": ""
}
```

### 76. GetGRNByName

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/GetGRNByName`

**Body params:**

- `grn`

**Sample body:**

```json
{
  "grn": ""
}
```

### 77. GetGRNExposureTime

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/GetGRNExposureTime`

**Body params:**

- `grnId`

**Returns:** ExposureTime – in seconds

**Sample body:**

```json
{
  "grnId": ""
}
```

### 78. GetMaterialByName

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/GetMaterialByName`

**Body params:**

- `Material`
- `Language_ID` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "Material": "",
  "Language_ID": ""
}
```

### 79. GetMSDExposureByGRN

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/GetMSDExposureByGRN`

**Body params:**

- `grn`

**Sample body:**

```json
{
  "grn": ""
}
```

### 80. InsertGRNBagHistory ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/InsertGRNBagHistory`

**Body params:**

- `usrId` — required and validated
- `grnId` — required and validated
- `grn`
- `bagOpened` — datetime of bag opening; can be “”
- `bagClosed` — datetime of bag closing; can be “”
- `bagStatus` — numeric value (sql int)
- `elapsedTime` — numeric value (sql int)
- `updateMode` — 0, 1, or 2

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "grnId": "",
  "grn": "",
  "bagOpened": "",
  "bagClosed": "",
  "bagStatus": "",
  "elapsedTime": "2025-01-01 00:00:00",
  "updateMode": "2025-01-01 00:00:00"
}
```

### 81. InsertGRNBakeHistory ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/InsertGRNBakeHistory`

**Body params:**

- `usrId` — required and validated
- `grnId` — required and validated
- `grn`
- `bakeIn` — datetime of bake in; can be “”
- `bakeOut` — datetime of bake out; can be “”
- `bakeStatus` — numeric value (sql int), 1 or other
- `elapsedTime` — numeric value (sql int)
- `bakeCompleted` — numeric value, 1 or other
- `bakeTemperature` — numeric value (sql int)

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "grnId": "",
  "grn": "",
  "bakeIn": "",
  "bakeOut": "",
  "bakeStatus": "",
  "elapsedTime": "2025-01-01 00:00:00",
  "bakeCompleted": "",
  "bakeTemperature": ""
}
```

### 82. UnblockGRN ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/UnblockGRN`

**Body params:**

- `grn` — required.
- `blockTypeId` — required.
- `blocked` — required.
- `assembly` — optional
- `revision` — optional
- `version` — optional
- `family` — optional
- `mfgArea` — optional
- `route` — optional
- `usrId` — required.

**Returns:** success/error

**Notes:**

- GRN must be previously blocked.
- Provide the corrects parameters depending on the BlockType_ID. For example, if
- BlockType_ID is 5, add in the parameters the name of family.

**Sample body:**

```json
{
  "grn": "",
  "blockTypeId": "",
  "blocked": "",
  "assembly": "",
  "revision": "",
  "version": "",
  "family": "",
  "mfgArea": "",
  "route": "",
  "usrId": ""
}
```

### 83. UpdateGRNBagStatus ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/UpdateGRNBagStatus`

**Body params:**

- `usrId`
- `grnId`
- `bagOpened`
- `bagClosed`
- `bagStatus` — Open or Close

**Returns:** success/error

**Notes:**

- bagStatus input
- Open
- To close the bag
- Set BagClosed to db time with offset regardless input parameter
- ElapsedExposureTime = GRN BagExposureTime – Time difference
- between BagOpened and BagClosed in mins
- bakeStatus = 01
- Close
- If BagOpened = '1900-01-01 00:00:00.000'
- To open the bag
- bagStatus = 1
- ElapsedExposureTime = GRN BagExposureTime
- Otherwise
- This is a reopen case
- Set BagOpened to db time with offset regardless input
- parameter
- Set BagClosed = '1900-01-01 00:00:00.000'
- bagStatus = 1
- ElapsedExposureTime = GRN BagExposureTime
- Update CR_GRNs table for the BagOpened/BagClosed, ElapsedExposureTime, and
- BagStatus
- Insert a record in CR_GRNBagHistory

**Sample body:**

```json
{
  "usrId": "",
  "grnId": "",
  "bagOpened": "",
  "bagClosed": "",
  "bagStatus": ""
}
```

### 84. UpdateGRNBagStatusByGRN ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/UpdateGRNBagStatusByGRN`

**Body params:**

- `usrId`
- `grn`
- `updateTime` — time to open or close grnBag
- `bagStatus` — Open or Close grn bag
- `elapsedTime` — optional; set to -1 if not updating GRN’s ElapsedExposureTime

**Returns:** success/error

**Notes:**

- Open bag
- Update CR_GRNs table for the GRN with
- bagOpened = updateTime
- bagClosed -- set to '1900-01-01 00:00:00.000' because until it is close it
- has the full exposure....
- bagStatus = 1
- ElapsedExposureTime = GRN exposureTime if elapsedTime is not null
- r -1.
- record inserted in CR_GRNBagHistory
- bagOpened = updateTime
- bagClosed = '1900-01-01 00:00:00.000'
- bagStatus = 1
- ElapsedExposureTime = GRN exposureTime by configuration
- Close bag
- Update CR_GRNs table for the GRN with
- bagOpened -- no change
- bagClosed = updateTime
- bagStatus = 0
- ElapsedExposureTime = no change
- record inserted in CR_GRNBagHistory
- bagOpened -- no change
- bagClosed = updateTime
- bagStatus = 0
- ElapsedExposureTime = GRN BagExposureTime – Time difference
- between BagOpened and BagClosed in mins

**Sample body:**

```json
{
  "usrId": "",
  "grn": "",
  "updateTime": "2025-01-01 00:00:00",
  "bagStatus": "",
  "elapsedTime": "2025-01-01 00:00:00"
}
```

### 85. UpdateGRNBakeStatus ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Material/UpdateGRNBakeStatus`

**Body params:**

- `usrId` — required and validated
- `grnId` — required and validated
- `grn`
- `bakeIn` — datetime of bake in; can be “”
- `bakeOut` — datetime of bake out; can be “”
- `bakeStatus` — “”, “Bake In”, or “Bake Out”
- `elapsedTime` — numeric value (sql int)
- `bakeCompleted` — numeric value, 1 or other
- `bakeTemperature` — numeric value (sql int); if “”, default to 0

**Returns:** success/error

**Notes:**

- bakeStatus input
- “” or “Bake Out”
- Set bakeIn to db time with offset regardless input parameter
- bakeOut = "1900-01-01 00:00:00.000"
- bakeStatus = 1
- Otherwise
- If “Bake In”
- bakeStatus = 0
- if bakeCompleted = 1
- bakeOut = db time with offset
- elapsedTime = GRNExposureTime*60
- otherwise,
- bakeout = "1900-01-01 00:00:00.000"
- Update CR_GRNs table for the bakeIn/BakeOut, BakeStatus, ElapsedExposureTime,
- and BakeTemperature
- Insert a record in CR_GRNBakeHistory
- Panel

**Sample body:**

```json
{
  "usrId": "",
  "grnId": "",
  "grn": "",
  "bakeIn": "",
  "bakeOut": "",
  "bakeStatus": "",
  "elapsedTime": "2025-01-01 00:00:00",
  "bakeCompleted": "",
  "bakeTemperature": ""
}
```


---

## PV

### 93. ProcessVerify ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/PV/ProcessVerify`

**Body params:**

- `custId`
- `usrId`
- `serial`
- `chkpt` — checkpoint name; can be “”
- `assemId`
- `assem`
- `rev`
- `procAsPanel`
- `langId`

**Returns:** success/error

**Notes:**

- Reporting

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "serial": "E50806012101079262600135",
  "chkpt": "",
  "assemId": "",
  "assem": "",
  "rev": "",
  "procAsPanel": "",
  "langId": "0"
}
```


---

## Panel

### 86. BreakupPanel ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/BreakupPanel`

**Body params:**

- `usrId` — required and validated
- `serial` — wip serial that is on the panel

**Returns:** success/error

**Notes:**

- all serials on the panel must have been birthed via other MES module before it can be
- broken.
- only set BoardPanelBroken == 1 if wips on the panel are not Scrapped or Purged, and
- stil attached to the panel

**Sample body:**

```json
{
  "usrId": "",
  "serial": "E50806012101079262600135"
}
```

### 87. GetPanelById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/GetPanelById`

**Body params:**

- `panelId`
- `langId` — “0” is not localized or English

**Returns:** Panel_ID, Panel, PanelName, BoardsPerPanel, Rows, Columns, NumberOfSides, SerialNumberType, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "panelId": "",
  "langId": "0"
}
```

### 88. GetPanelByWipID

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/GetPanelByWipID`

**Body params:**

- `usrId`
- `wipId`
- `langId` — “0” is not localized or English

**Returns:** Panel_ID, Panel, WIP_ID, SerialNumber, Mapping, XOut, XOutStats

**Sample body:**

```json
{
  "usrId": "",
  "wipId": "1144749",
  "langId": "0"
}
```

### 89. ListConnectedSerialInPanel

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/ListConnectedSerialInPanel`

**Body params:**

- `panelId`

**Returns:** Panel_ID, Panel, WIP_ID, SerialNumber, Mapping, XOut, XOutStats

**Sample body:**

```json
{
  "panelId": ""
}
```

### 90. ListConnectedSerialInPanelWithCoordinates

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/ListConnectedSerialInPanelWithCoordinates`

**Body params:**

- `panelId`

**Returns:** Panel_ID, Panel, WIP_ID, SerialNumber, Mapping, XOut, XOutStats, BoardRow, BoardColumn

**Sample body:**

```json
{
  "panelId": ""
}
```

### 91. ListPanelMappingById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/ListPanelMappingById`

**Body params:**

- `panelId`
- `langId` — “0” is not localized or English – optional

**Returns:** Panel_ID, Panel, PanelText, Mapping, BoardRow, BoardColumn, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "panelId": "",
  "langId": "0"
}
```

### 92. ListPanelSerialByWipId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Panel/ListPanelSerialByWipId`

**Body params:**

- `wipId`

**Returns:** Panel_ID, Panel, WIP_ID, SerialNumberOri, Mapping, XOut, XOutStats, SerialNumber

**Notes:**

- PV

**Sample body:**

```json
{
  "wipId": "1144749"
}
```


---

## Reporting

### 94. AgingWipsReportByCustomer

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Reporting/AgingWipsReportByCustomer`

**Body params:**

- `custId`
- `langId`

**Returns:** SerialNumber – wip serial number, IsRMA – whether the wip is a RMA, RMAReturnCount – # of time this wip has returned as RMA, Batch – batch associated with specific route step move, FamilyText, Assembly_ID, AssemblyNumber, AssemblyRevision, Route, CurrentStep, CurrentRouteStep, LastWipMoveTime – last datetime in WP_AssemblyRouteWip for this wip/routestep, AgingDays – days since LastWipMoveTime (above)

**Notes:**

- Please work with your application admin to make sure sure the web.config of api have
- the following setting:
- configuration setting, sqlReportingTimeout, is used to alter SQL command
- timeout for GetReport method
- Timeout in seconds.
- E.g. 300. Please note that 0 will not be accepted. In the case 0 is set,
- the api will assume default behavior of 30 secs timeout

**Sample body:**

```json
{
  "custId": "0",
  "langId": "0"
}
```

### 95. GetReport

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Reporting/GetReport`

**Body params:**

- `rptSPName`
- `inputJson`
- `usrId`

**Returns:** return of customized stored procedure as “rptSPName”

**Notes:**

- please refer to document, “ MESWebApi – writing customized sp to get reporting data
- via generic api call ” for implementation details
- Route

**Sample body:**

```json
{
  "rptSPName": "",
  "inputJson": "",
  "usrId": ""
}
```


---

## Route

### 96. ListRouteAssemblyByCustomer

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListRouteAssemblyByCustomer`

**Body params:**

- `customerId` — string[], e.g., [2,3]
- `routeStatus` — e.g., “Active”
- `updatedAfter` — e.g., “2019-07-01”
- `langId` — “0” is not localized or English

**Returns:** CustomerName, DivisionName, AssemblyNumber, Route, AssemblyRevision, RouteStatus, UpdatedDate

> ⚠️ Verified 2026-08-18. `customerId` really is an **array of strings** — a bare
> `"51"` returns `400 customerId(s) cannot be null.` `updatedAfter` must be
> **`yyyy-mm-dd`** (no time part) — `"2020-01-01 00:00:00"` returns
> `400 updatedAfter date format does not match yyyy-mm-dd.` A wide window
> (2020→today) read-timed-out at 30 s; keep it narrow or raise the timeout.
> For a single customer prefer **97 `ListRouteAssemblyByCustomerId`**.

**Sample body:**

```json
{
  "customerId": ["51"],
  "routeStatus": "Active",
  "updatedAfter": "2025-01-01",
  "langId": "0"
}
```

### 97. ListRouteAssemblyByCustomerId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListRouteAssemblyByCustomerId`

> ⚠️ **The source PDF is wrong here.** Its “Api/Method” line for §97 says
> `Route/ListRouteAssemblyByCustomer` — a copy-paste of §96. The real method is
> `ListRouteAssemblyByCustomer`**`Id`** (the PDF's own heading and table of
> contents both say so). Confirmed working against prod 2026-08-18.
> These are **two different methods**: 96 takes a `customerId` **array**,
> 97 takes a single `custId`. Sending 97's body to 96's URL returns
> `400 customerId(s) cannot be null.`

**Body params:**

- `custId` — valid customer_ID
- `activeRouteOnly` — 0 or 1; default to 1
- `updatedAfter` — optional; if omitted, returns data changed in past 30 days
- `langId` — “0” is not localized or English

**Returns:** Customer_ID, CustomerName, DivisionName, AssemblyNumber, AssemblyRevision, Route, RouteStatus, UpdatedDate

**⚠️ The documented response is INCOMPLETE and partly WRONG.** Verified against
prod 2026-08-18 with `custId=51` (ADVANTEST):

| Field | PDF says | Actually returns |
|---|---|---|
| `Customer_ID` | the customer id | **always `null`** — unusable |
| `AssemblyNumber` | the assembly number | **the REVISION** (`"001"`, `"014"`) — the real number (`0380-1061`) is **not returned at all**. Cross-check with `Assembly/ListAssembly`. |
| `AssemblyRevision` | the revision | same value as `AssemblyNumber` — duplicate |
| `Route` | a route name | a **composite id path** `Factory_ID/MA_ID/Route_ID/RouteStep_ID`, e.g. `2/231/2160/45213`. The 4th part joins **1:1** to `RouteStep_ID` from `Route/ListRouteStep` (1809/1809 hit). |
| — | not documented | **`Factory`** (`P1`) — undocumented |
| — | not documented | **`ManufacturingArea`** (`Cell 4`, `Bay 05B`) — undocumented |
| — | not documented | **`RouteText`** (`SuperCellR05B`) — undocumented, and this is the **real route name** that joins to `ListRouteStep.RouteName` |

**Grain:** one row per (assembly × route-step), *not* per assembly. ADVANTEST returned
**84,678 rows** for **19 assemblies** / 8 routes — and only **8,726** are unique on
`(AssemblyNumber, AssemblyRevision, Route)`, so **≈10× duplication**. Always dedupe.

**Practical use:** this is the cheapest **workcell → configured route-steps** call —
one request, no per-assembly loop. Take `RouteText` (or the 4th part of `Route`) and
join to `Route/ListRouteStep`.

**Sample body:** (verified — ADVANTEST, `custId` 51)

```json
{
  "custId": "51",
  "activeRouteOnly": 0,
  "langId": "0"
}
```

### 98. ListRouteStep

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListRouteStep`

**Body params:**

- `factory` — “” if don’t want to specify factoryname
- `langId` — “0” is not localized or English – optional

**Returns:** RouteStep_ID, FactoryMARoute_ID, FactoryName, ManufacturingAreaName, RouteName, Step_ID, StepName, Descr, Description, Occurrence, StepOrder, XCoordinate, YCoordinate, StepType, StepTypeName, NextStep_ID, RouteValidation, BirthingStation, BackFlush, UseJUID, WorkCenter_ID, WorkCenterText, UserID_ID, LastUpdated, CheckDate

> ⚠️ **No customer/workcell parameter exists.** `factory:""` returns the **whole site**:
> **91,010 rows** — P1 55,414 · BK 11,455 · P2 10,528 · P3 6,163 · P4 3,341 · P8 1,363 ·
> P6 1,279 · GP 1,013 · P5 260 · (+ a few strays: `Ghost`, `P1B`, `ELFAC01/02`, `JABIL`).
> To scope it to a workcell, first call **97 `ListRouteAssemblyByCustomerId`** and filter
> on `RouteName == RouteText`. Verified 2026-08-18.
>
> Unlike **99 `ListRouteStepByFactoryMARoute`**, `StepType`/`StepTypeName`/`WorkCenter_ID`
> ARE populated here. `Description` = the **step instance** (the join key to scan data);
> `StepName` is the generic type. ADVANTEST: 1,857 configured steps → 1,190 distinct
> `Description`, only 72 distinct `StepName`.

**Sample body:**

```json
{
  "factory": "",
  "langId": "0"
}
```

### 99. ListRouteStepByFactoryMARoute

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListRouteStepByFactoryMARoute`

**Body params:**

- `fmaRouteId` — required and validated
- `step`
- `langId` — “0” is not localized or English – optional

**Returns:** RouteStep_ID, StepName, Descr, Description, Occurrence, StepOrder, StepType,, XCoordinate, YCoordinate, Step_ID, WorkCenter_ID, StepType_IDte

**Sample body:**

```json
{
  "fmaRouteId": "",
  "step": "",
  "langId": "0"
}
```

### 100. ListRouteStepByStepEquipmentAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListRouteStepByStepEquipmentAssembly`

**Body params:**

- `stepInstance`
- `equipId`
- `assem` — assembly number
- `rev` — assembly revision

**Returns:** RouteStep_ID, FactoryText, MAText, RouteText, StepText, DescrText

**Sample body:**

```json
{
  "stepInstance": "",
  "equipId": "",
  "assem": "",
  "rev": ""
}
```

### 101. ListSubWorkCenter

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListSubWorkCenter`

**Body params:**

- `stepInstance`
- `Customer`
- `Division`

**Sample body:**

```json
{
  "stepInstance": "",
  "Customer": "",
  "Division": ""
}
```

### 102. ListProcessLink

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Route/ListProcessLink`

**Body params:**

- `Customer`
- `Division`
- `AssemblyNumber`
- `AssemblyRevision`
- `StepInstance`

**Sample body:**

```json
{
  "Customer": "",
  "Division": "",
  "AssemblyNumber": "",
  "AssemblyRevision": "",
  "StepInstance": ""
}
```


---

## RouteStepSetup

### 103. GetRouteStepComponentUseBin

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/GetRouteStepComponentUseBin`

**Body params:**

- `routeStepId`
- `bin`
- `grnId` — ,
- `loadTime`
- `langId` — “0” is not localized or English

**Sample body:**

```json
{
  "routeStepId": "",
  "bin": "",
  "grnId": "",
  "loadTime": "2025-01-01 00:00:00",
  "langId": "0"
}
```

### 104. ListActiveRouteStepSetupByRouteStepAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/ListActiveRouteStepSetupByRouteStepAssembly`

**Body params:**

- `routeStepId`
- `assemId`
- `langId` — “0” is not localized or English – optional

**Returns:** RouteStepSetup_ID, Assembly_ID, Number, Revision, Version, AssemblyName, BOM_ID, RouteStep_ID, FactoryName, FactoryMAName, RouteName, StepName, SetupNumber, SetupVersion, TotalComponents, TotalPins, Alias, Export, Active, RFDisplay, MachineName, ProgramName, LoadDateTime, Assembly, CommonRouteStepSetup_ID, CommonSetupName, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "routeStepId": "",
  "assemId": "",
  "langId": "0"
}
```

### 105. ListRouteStepComponentById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/ListRouteStepComponentById`

**Body params:**

- `routeStepId`
- `bin`
- `langId` — “0” is not localized or English – optional

**Returns:** RouteStep_ID, FactoryName, ManufacturingAreaName, RouteName, StepName, Bin, Feeder, Tray, Track, GRN_ID, GRN, Material, Qty, UserID_ID, LastUpdated, CheckDateQty

**Sample body:**

```json
{
  "routeStepId": "",
  "bin": "",
  "langId": "0"
}
```

### 106. ListRouteStepSetupBinBySetup

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/ListRouteStepSetupBinBySetup`

**Body params:**

- `setupId` — routestepsetup_id
- `langId` — “0” is not localized or English – optional

**Returns:** RouteStepSetupBin_ID, RouteStepSetup_ID, CommonRouteStepSetup_ID, CommonSetupName, Assembly_ID, Number, Revision, Version, AssemblyName, RouteStep_ID, FactoryName, ManufacturingAreaName, RouteName, StepName, Bin, Feeder, Tray, Track, Material_ID, Material, Qty, OutOfStock, Deviation, UserID_ID, LastUpdated, CheckDateQty

**Sample body:**

```json
{
  "setupId": "",
  "langId": "0"
}
```

### 107. RouteStepComponentAddPart ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/RouteStepComponentAddPart`

**Body params:**

- `usrId` — required and validated
- `setupId` — routestepsetup_ID
- `routeId`
- `bin`
- `grnId`
- `qty`

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "setupId": "",
  "routeId": "",
  "bin": "",
  "grnId": "",
  "qty": ""
}
```

### 108. RouteStepComponentRemovePart ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/RouteStepComponentRemovePart`

**Body params:**

- `usrId` — required and validated
- `routeStepId`
- `bin` — ,
- `grnId`
- `reelIsEmpty`

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "routeStepId": "",
  "bin": "",
  "grnId": "",
  "reelIsEmpty": ""
}
```

### 109. RouteStepSetupValidation

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/RouteStepSetup/RouteStepSetupValidation`

**Body params:**

- `setupId` — routeStepSetup_ID

**Returns:** success/error

**Notes:**

- Security

**Sample body:**

```json
{
  "setupId": ""
}
```


---

## Security

### 110. GetUserByUserId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Security/GetUserByUserId`

**Body params:**

- `usrId` — UserID_ID

**Returns:** UserID_ID, UserID, WindowsUserID, LastName, FirstName, Active

**Notes:**

- Valid and active MES user

**Sample body:**

```json
{
  "usrId": ""
}
```

### 111. GetUserByWindowsId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Security/GetUserByWindowsId`

**Body params:**

- `user` — windows NT userId; if service account is used, it will be the name of the

**Returns:** UserID_ID, UserID, WindowsUserID, LastName, FirstName, Active

**Notes:**

- Valid and active MES user
- This is the first api to call before each session. Client should cache UserID_ID in the
- front end. Most of “wirte” api’s (insert or update database) will need to pass in this
- parameter (usrId)
- Return UserID_ID for active user only

**Sample body:**

```json
{
  "user": ""
}
```

### 112. ListGroupsByUserId

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Security/ListGroupsByUserId`

**Body params:**

- `UserID_ID`
- `PartialKey` — use this to narrow the groups that are returnned

**Returns:** Group_ID, GroupDescr – Security group name, UserID_ID, UserId, LastName, FirstName, AuditUserID_ID, CheckDate

**Notes:**

- Serial

**Sample body:**

```json
{
  "UserID_ID": "",
  "PartialKey": ""
}
```


---

## Serial

### 113. GenerateSerialNumbers ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Serial/GenerateSerialNumbers`

**Body params:**

- `custId` — required and validated
- `usrId` — required and validated
- `serialName` — SerialNumberName in MES
- `qty` — 1+
- `assemId` — Assembly_ID for the serial number
- `fmaId` — FactoryMA_ID
- `batchId` — valid or 0
- `example` — = "0" if not testing/sampling
- `langId` — “0” is not localized or English – optional

**Returns:** List of (RowNumber, SerialNumber)

**Notes:**

- Use MES to configure serialnumber
- If example is set to “0” (default), serial numbers will be generated and inserted into
- SerialNumberLog table.

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "serialName": "E50806012101079262600135",
  "qty": "",
  "assemId": "",
  "fmaId": "",
  "batchId": "",
  "example": "",
  "langId": "0"
}
```

### 114. GetBarcodeMaskById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Serial/GetBarcodeMaskById`

**Body params:**

- `barcodeMaskId`
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "barcodeMaskId": "",
  "langId": "0"
}
```


---

## Site

### 115. ListCustomer

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Site/ListCustomer`

**Body params:**

- `partialKey` — required
- `active` — = "1" if active only
- `langId` — “0” is not localized or English – optional

**Returns:** Customer_ID, Customer, CustomerName, Division, DivisionName, CustomerDivisionName, SAPIdentifier, ForceValidGRN, ProhibitLastPart, RequireQuickscan, UserID_ID, System.DateTime LastUpdated, CheckDate, Active

**Notes:**

- To get active customer, set active = “1”.
- Test

**Sample body:**

```json
{
  "partialKey": "",
  "active": "1",
  "langId": "0"
}
```


---

## Test

### 116. GetCurrentRouteStep

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetCurrentRouteStep`

**Body params:**

- `serialNumber` — required

**Sample body:**

```json
{
  "serialNumber": "E50806012101079262600135"
}
```

### 117. GetLastTestResult

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetLastTestResult`

**Body params:**

- `serialNumber` — required
- `customerName` — required
- `division`
- `processStep` — required

**Returns:** StartTime, StopTime, TestStatus, MachineName, Operator, StepOrTestName, FailureLabel, FailureMessage

**Sample body:**

```json
{
  "serialNumber": "E50806012101079262600135",
  "customerName": "",
  "division": "",
  "processStep": ""
}
```

### 118. GetPanelSerializeResult

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetPanelSerializeResult`

**Body params:**

- `customerName`
- `division`
- `panelSerialNumber` — required

**Returns:** Panel_ID, Panel, WIP_ID, SerialNumberOri, Mapping, XOut, XOutStats, SerialNumber

**Sample body:**

```json
{
  "customerName": "",
  "division": "",
  "panelSerialNumber": "E50806012101079262600135"
}
```

### 119. GetProcessStepFailLoops

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetProcessStepFailLoops`

**Body params:**

- `customerName`
- `division` — required
- `serialNumber`
- `processStep`

**Returns:** ConsecutiveFailCount – consecutive fail count since last “Pass” or absent at the, processStep, MaxLoopCount – max loop count configured for processStep, LastOpStatus – last process status for the wip, LastProcessStep – last process step name

**Sample body:**

```json
{
  "customerName": "",
  "division": "",
  "serialNumber": "E50806012101079262600135",
  "processStep": ""
}
```

### 120. GetTestDataFormats

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetTestDataFormats`

**Sample body:**

```json
{}
```

### 121. GetTestHistory

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetTestHistory`

**Body params:**

- `serialNumber` — required
- `customerName` — required
- `division`
- `processStep` — required

**Returns:** StartTime, StopTime, TestStatus, MachineName, Operator, StepOrTestName, FailureLabel, FailureMessage

**Sample body:**

```json
{
  "serialNumber": "E50806012101079262600135",
  "customerName": "",
  "division": "",
  "processStep": ""
}
```

### 122. GetTestHistoryWithDefects

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/GetTestHistoryWithDefects`

**Body params:**

- `serialNumber` — required
- `customerName` — required
- `division`
- `langId` — “0” is not localized or English – optional

**Returns:** StartTime, StopTime, TestStatus, MachineName, Operator, StepOrTestName, Process_ID, Equipment_ID, WIP_ID, RouteStep_ID, Assembly_ID, AssemblyNumber, Assembly, FactoryMARoute_ID, FactoryText, MAText, RouteText, Test_Process, Family_ID, FamilyText, FailureLabels: Null, or List of:, Data_ID, FailureLabel, FailureMessage, Analysis: null, or List of:, Analysis_ID, AnalysisDateTime, DefectLocation, Defect_ID, Defect, DefectCategory_ID, DefectCategory, AnalysisEquipmentOrRouteStepSetup_ID, AnalysisEquipmentOrRouteStepSetup, AnalysisIsEquipmentSetup, Material_ID, Material, DefectCount, FeederTrayTrack_ID, DefectDetail, AnalysisRouteStep_ID, AnalysisRouteStep, AnalysisOperator, Repairs: Null, or List of:

**Sample body:**

```json
{
  "serialNumber": "E50806012101079262600135",
  "customerName": "",
  "division": "",
  "langId": "0"
}
```

### 123. InsertAnalysis ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/InsertAnalysis`

**Body params:**

- `WIP_ID`
- `Process_ID`
- `Data_ID`
- `langId` — “0” is not localized or English – optional
- `userId` — optional, defaults to 1 (system account) if not used
- `AnalysisRouteStep_ID` — optional, see notes on assigned route step
- `Analysis_Factory` — optional, see notes on assigned route step
- `Analysis_MA` — optional, see notes on assigned route step
- `Analysis_Route` — optional, see notes on assigned route step
- `Analysis_Step` — optional, see notes on assigned route step
- `Analysis_StepInstance` — optional, see notes on assigned route step
- `analysis`

**Sample body:**

```json
{
  "WIP_ID": "",
  "Process_ID": "",
  "Data_ID": "",
  "langId": "0",
  "userId": "",
  "AnalysisRouteStep_ID": "",
  "Analysis_Factory": "",
  "Analysis_MA": "",
  "Analysis_Route": "",
  "Analysis_Step": "",
  "Analysis_StepInstance": "",
  "analysis": ""
}
```

### InsertRework ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/InsertRework`

**Body params:**

- `WIP_ID`
- `Process_ID`
- `Data_ID`
- `langId` — “0” is not localized or English – optional
- `Analysis_ID`
- `userId` — optional, defaults to 1 (system account) if not used
- `ReworkRouteStep_ID` — optional, see notes on assigned route step
- `Rework_Factory` — optional, see notes on assigned route step
- `Rework` — _MA – optional, see notes on assigned route step
- `Rework_Step` — optional, see notes on assigned route step
- `Rework_StepInstance` — optional, see notes on assigned route step

**Returns:** Analysis_ID

**Notes:**

- The following fields are verified and should match a value in MES
- analysis/cause – Should match a defect in MES
- analysis/crd_part_number – Refers to a material, should match a material in MES
- Notes on selecting route step: By default, the analysis is assigned to the same route step as the test
- being referenced. You can override this by passing in one of two values:
- AnalysisRouteStep_ID: Points to the exact route step ID you wish to reference
- Analysis_Factory, Analysis_MA, Analysis_Route, Analysis_Step, Analysis_StepInstance: This will
- query the route based on text passed in for factory, manufacturing area (“MA”), route, step, and
- step instance / description. If the route step ID cannot be found, an error will return.
- If both these values are passed in, AnalysisRouteStep_ID will override the text lookup parameters.
- Notes on selecting feeder / bins: By default, the MES Web API will try to compute the equipment or
- route setup and feeder / bin based on the CRD and WIP passed in. The web API will take the first
- result that matches. If this cannot be computed, this will be left blank (0 IDs) when writing the
- analysis record.
- Optionally, you can pass in these values directly to the API.
- For equipment setup / feeders, “EquipmentOrRouteStep_ID” should be set to the
- EquipmentSetup_ID of the equipment setup, “IsEquipment” should be 1, and “FeederTrayTrack_ID”
- should point to the ID of the feeder / tray / track where the CRD is.
- If you are passing in route step setup / bins, “EquipmentOrRouteStep_ID” should be set to the
- RouteStepSetup_ID of the route step setup, “IsEquipment” should be 0, and “FeederTrayTrack_ID”
- should be the Bin value (found in CT_RouteStepSetupBins) where the CRD is.
- InsertRework
- Api/Method:
- Test/InsertRework
- Input Paramters:
- [FromBody]
- WIP_ID
- Process_ID
- Data_ID
- langId– “0” is not localized or English – optional
- Analysis_ID
- userId – optional, defaults to 1 (system account) if not used
- ReworkRouteStep_ID – optional, see notes on assigned route step
- Rework_Factory – optional, see notes on assigned route step
- Rework _MA – optional, see notes on assigned route step
- Rework _Route – optional, see notes on assigned route step
- Rework_Step – optional, see notes on assigned route step
- Rework_StepInstance – optional, see notes on assigned route step
- rework
- Single record (optional) with:
- defect_id (note: currently not used, pass in 0)

**Sample body:**

```json
{
  "WIP_ID": "",
  "Process_ID": "",
  "Data_ID": "",
  "langId": "0",
  "Analysis_ID": "",
  "userId": "",
  "ReworkRouteStep_ID": "",
  "Rework_Factory": "",
  "Rework": "",
  "Rework_Step": "",
  "Rework_StepInstance": ""
}
```

### 124. ListDefects

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/ListDefects`

**Body params:**

- `UserID_ID` — Must be an active user ID in MES
- `PartialKey` — Filters defect list
- `ActiveOnly` — “true” or “false”
- `MaxCount` — Optional, use 0 to retrieve everything
- `Language_ID` — “0” is not localized or English – optional

**Returns:** Defect_ID, Defect, DefectText, DefectCategory_ID, DefectCategory, Active

**Notes:**

- Returns a list of the current defect options, and their categories, in MES.

**Sample body:**

```json
{
  "UserID_ID": "",
  "PartialKey": "",
  "ActiveOnly": "1",
  "MaxCount": "",
  "Language_ID": ""
}
```

### 125. ListReworkCategories

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/ListReworkCategories`

**Body params:**

- `partialKey` — Filters rework list
- `maxCount` — Optional, use 0 to retrieve everything
- `langId` — “0” is not localized or English – optional

**Returns:** RepairCategory_ID, RepairCategoryText, RequiresMRBLabel, EnableGRNField

**Sample body:**

```json
{
  "partialKey": "",
  "maxCount": "",
  "langId": "0"
}
```

### 126. ListTestDataWithinTime

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/ListTestDataWithinTime`

**Body params:**

- `startTime` — required in datetime format
- `endTime` — required in datetime format
- `maxCount` — optional, default = 400

**Returns:** ExtractionStartTime, ExtractionEndTime, Customer, Division, Assembly, Revision, RouteText, StepText, DescrText, SerialNumber, TestStatus, ProcessLoop, StartDateTime, StopDateTime

**Notes:**

- The time between startTime and endTime is limited to 30 minutes

**Sample body:**

```json
{
  "startTime": "2025-01-01 00:00:00",
  "endTime": "2025-01-01 00:00:00",
  "maxCount": ""
}
```

### 128. OKToTestLinkMaterial

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/OKToTestLinkMaterial`

**Body params:**

- `customerName` — required
- `Division`
- `linkMaterialSerialNumber` — required
- `sssemblyNumber`
- `testerName` — required
- `processStep`

**Sample body:**

```json
{
  "customerName": "",
  "Division": "",
  "linkMaterialSerialNumber": "E50806012101079262600135",
  "sssemblyNumber": "",
  "testerName": "",
  "processStep": ""
}
```

### 129. OkToTest_Breakout

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/OkToTest_Breakout`

**Body params:**

- `customerName`
- `division`
- `serialNumber` — required
- `assemblyNumber`
- `testerName` — required
- `processStep`
- `breakOutFullPanel` — required
- `userId` — required

**Sample body:**

```json
{
  "customerName": "",
  "division": "",
  "serialNumber": "E50806012101079262600135",
  "assemblyNumber": "",
  "testerName": "",
  "processStep": "",
  "breakOutFullPanel": "",
  "userId": ""
}
```

### 130. ProcessTestData ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Test/ProcessTestData`

**Body params:**

- `testData`
- `dataFormat`

**Returns:** SUCCESS/ FAIL, Wip

**Sample body:**

```json
{
  "testData": "",
  "dataFormat": ""
}
```


---

## Wip

### 131. AssemblyRouteWipInsert ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/AssemblyRouteWipInsert`

**Body params:**

- `usrId` — required and validated
- `assemId`
- `fmaRouteId` — FactoryMARoute_ID
- `wipId`
- `batchId` — BatchID_ID
- `processSingleBoardOnPanel` — = "0" if process entire panel

**Returns:** success/error

**Sample body:**

```json
{
  "usrId": "",
  "assemId": "",
  "fmaRouteId": "",
  "wipId": "1144749",
  "batchId": "",
  "processSingleBoardOnPanel": ""
}
```

### 132. BoardHistoryReport

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/BoardHistoryReport`

**Body params:**

- `custId` — required and validated
- `serial` — serial
- `useMultiPartBarCode` — 0 if no using multiple part barcode
- `lang` — 0 if English

**Returns:** TestType, SerialNumber, Number, Assembly, Revision, Version, FactoryText, MAText, RouteText, Test_Process, TestStatus, DefectLocation, No_Of_Defects, PinCount, StartDateTime, StopDateTime, EquipmentRouteName, Material, Defect_ID, Defect, DefectCategory, Customer, LastName, FirstName, DefectDetail, Memo, RepairStatus, RepairFlag, RepairDateTime, RepairCategory, RepairedByFirstName, RepairedByLastName, DeviationNumber, DeviationFromDate, DeviationToDate, DevitationType, FixtureText, FixtureSlot, FamilyText, ChildSerialNumber, ParentSerialNumber, ODBCTime, FailureLabel, MeasuredData, MeasuredUnit, DataRec_ID

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "useMultiPartBarCode": "",
  "lang": ""
}
```

### 133. GetWipBySerial

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipBySerial`

**Body params:**

- `custId` — custId must be valid and not “0”
- `serial`
- `useMultiPartBarCode`
- `autoTranslate`

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "useMultiPartBarCode": "",
  "autoTranslate": ""
}
```

### 134. GetWipBySerial

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipBySerial`

**Body params:**

- `serial`
- `useMultiPartBarCode`
- `autoTranslate`

**Returns:** Wip_ID, SerialNumber, Customer_ID, SeqNumber, Panel_ID, Panel, ProcessAsPanel, Active, Purge, ReferenceUnit, BirthStatus, ReferenceUnitActive, Scrap, OnHold, CurrentlyOnHold, RMA, RMAReturnCount, Deviation, MultiPartBarCode, BoardPanelBroken, Assembly_ID, Number, Revision, Version, BuildStatus_ID, BuildStatusText, Deviation_ID, BatchID_ID, BatchID, PreviousAssembly_ID, NextAssembly_ID

**Notes:**

- This differs from GetBoardData in that if the same serial number points to multiple
- boards, all the boards will return. Serial number is unique by customer only.

**Sample body:**

```json
{
  "serial": "E50806012101079262600135",
  "useMultiPartBarCode": "",
  "autoTranslate": ""
}
```

### 135. GetBoardLinkedData

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetBoardLinkedData`

**Body params:**

- `custId` — required and validated
- `serial` — serial
- `langId` — “0” is not localized or English – optional

**Returns:** Wip_ID, ChildWip_ID, LinkData_, LinkData, LinkMaterial_ID, LinkObject, LinkObjectRevisionUserID_ID, LastUpdated, IsAssembly

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "langId": "0"
}
```

### 136. GetParentBoard

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetParentBoard`

**Body params:**

- `custId` — required and validated
- `serial` — wip serial that is on the panel

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135"
}
```

### 137. GetWipBirthAndCurrentInfo

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipBirthAndCurrentInfo`

**Body params:**

- `usrId`
- `custId`
- `serial`
- `birthStatus`
- `active`
- `langId` — “0” is not localized or English – optional

**Sample body:**

```json
{
  "usrId": "",
  "custId": "0",
  "serial": "E50806012101079262600135",
  "birthStatus": "",
  "active": "1",
  "langId": "0"
}
```

### 138. GetWipBatchNextAssembly

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipBatchNextAssembly`

**Body params:**

- `custId`
- `serial`
- `routeStepId`

**Returns:** NextAssembly_ID

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "routeStepId": ""
}
```

### 139. GetWipBySerial

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipBySerial`

**Body params:**

- `custId` — custId must be valid and not “0”
- `serial`
- `useMultiPartBarCode`
- `autoTranslate`

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "useMultiPartBarCode": "",
  "autoTranslate": ""
}
```

### 140. GetWipComponentUsageFromComponent

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipComponentUsageFromComponent`

**Body params:**

- `Material`
- `StartTime`
- `EndTime`
- `Language_ID` — “0” is not localized or English – optional

**Returns:** Assembly, Quantity, WIP_ID, SerialNumber, Active, Purge, Scrap, OnHold, CurrentlyOnHold, RMA, Deviation, GRN, CurrentRoute, CurrentStep, LastWIPUpdate, ParentWIP_ID, ParentSerialNumber, ParentActive, ParentPurge, ParentScrap, ParentOnHold, ParentCurrentlyOnHold, ParentRMA, ParentDeviation, ParentCurrentRoute, ParentCurrentStep, ParentLastWIPUpdate

**Notes:**

- Please work with your application admin to make sure sure the web.config of api have
- the following setting:
- configuration setting, sqlReportingTimeout, is used to alter SQL command
- timeout for GetReport method
- Timeout in seconds.
- E.g. 300. Please note that 0 will not be accepted. In the case 0 is set,
- the api will assume default behavior of 30 secs timeout

**Sample body:**

```json
{
  "Material": "",
  "StartTime": "2025-01-01 00:00:00",
  "EndTime": "2025-01-01 00:00:00",
  "Language_ID": ""
}
```

### 141. GetWipHoldHistory

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/GetWipHoldHistory`

**Body params:**

- `custId`
- `serial`

**Returns:** CustomerText, SerialNumber, HoldTypeText, HoldTypeDescription, HoldDateTime, HoldBy, HoldMemo, ReleaseDateTime, ReleaseBy, ReleaseMemo

**Notes:**

- Wip hold history in HoldDateTime descending order

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135"
}
```

### 142. ListAssemblyRouteWip

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ListAssemblyRouteWip`

**Body params:**

- `wipId`
- `langId` — “0” is not localized or English

**Returns:** Customer_ID, CustomerText, DivisionText, Assembly_ID, AssemblyText, Number, Revision, Version, Family_ID, FamilyText, FactoryMARoute_ID, FactoryText, MAText, RouteText, WIP_ID, SerialNumber, Active, BatchID_ID, BatchID, UserID_ID, LastUpdated, CheckDate

**Sample body:**

```json
{
  "wipId": "1144749",
  "langId": "0"
}
```

### 143. ListCRDs

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ListCRDs`

**Body params:**

- `wip_Id`
- `assembly_Id` — optional, can pass 0
- `associateDeviation` — optional, can pass blank string
- `partialKey` — optional, used to filter CRD list
- `maxCount` — optional, can pass 0 to return all results

**Returns:** CRD, Material_ID, Material, PinCount, EquipmentOrRouteStep_ID, IsEquipment, Equipment, Feeder, Tray, Track, FeederTrakTrack_ID, ComponentType, GRN_ID, GRN, Descr, RouteStepSetup, RouteStep_ID, Equipment_ID

**Notes:**

- WIP/ListCRDs depends on the MES stored procedure up_QM_ListCRDsWithMoreInfo
- Please check if this stored procedure is the May 27 2014 or higher
- (In MES, you can partial key search CRDs in the CRD drop down in manual inspection)
- If this stored procedure is old, you may have to upgrade MES before using this method.
- If you use an out of date procedure, you may receive this error:
- Error: up_QM_ListCRDsWithMoreInfo has too many arguments specified

**Sample body:**

```json
{
  "wip_Id": "",
  "assembly_Id": "",
  "associateDeviation": "",
  "partialKey": "",
  "maxCount": ""
}
```

### 144. ListWipMeasuredDataByDataLabel

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ListWipMeasuredDataByDataLabel`

**Body params:**

- `custId`
- `serial`
- `dataLabel`

**Returns:** SerialNumber, DataLabel, MeasuredData, LastUpdated

**Notes:**

- Wip MeasuredData of specific DataLabel for a wip in LastUpdated descending order.

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "dataLabel": ""
}
```

### 145. ListWipRouteStepById

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ListWipRouteStepById`

**Body params:**

- `wipId`
- `langId` — “0” is not localized or English – optional

**Returns:** WIP_ID, SerialNumber, RouteStep_ID, FactoryText, MAText, RouteText, StepText, Equipment_ID, Vendor, Model, CommonName, StartTime, EndTime, OpStatus_ID, OpStatusText, StartUserID_ID, EndUserID_ID

**Sample body:**

```json
{
  "wipId": "1144749",
  "langId": "0"
}
```

### 146. ListWipRouteStepBySerial

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ListWipRouteStepBySerial`

**Body params:**

- `Serial` — wip serial
- `langId` — “0” is not localized or English – optional

**Returns:** WIP_ID, SerialNumber, RouteStep_ID, FactoryText, MAText, RouteText, StepText, Equipment_ID, Vendor, Model, CommonName, StartTime, EndTime, OpStatus_ID, OpStatusText, StartUserID_ID, EndUserID_ID

**Sample body:**

```json
{
  "Serial": "E50806012101079262600135",
  "langId": "0"
}
```

### 147. LocateBoard

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/LocateBoard`

**Body params:**

- `custId`
- `serial` — board, child board or unique linkdata

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135"
}
```

### 148. PlaceBoardOnHold ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/PlaceBoardOnHold`

**Body params:**

- `usrId` — required and validated
- `wipId` — required
- `holdTypeId` — required; used hold type configured for specific operation
- `processId` — = "0" if none
- `memo` — required; “” if not memo intended

**Returns:** success/error

**Notes:**

- subject to hold validation
- wip must be valid and still in process. E.g., a packed wip cannot be put on hold.
- Wip cannot be put on hold if it is already on hold for the same hold type.
- Set Process_ID = 0
- Caller client is responsible to figure out HoldType_ID to be used.

**Sample body:**

```json
{
  "usrId": "",
  "wipId": "1144749",
  "holdTypeId": "",
  "processId": "",
  "memo": ""
}
```

### 149. ReleaseBoardFromHold ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ReleaseBoardFromHold`

**Sample body:**

```json
{}
```

### 150. ScrapWip

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ScrapWip`

**Body params:**

- `custId`
- `serial`
- `usrId`

**Returns:** success/error

**Notes:**

- Serial number must not have child component
- Serial number must not be packed
- Serial number must no be associated to an order

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "usrId": ""
}
```

### 151. ValidateWipBarcode

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/ValidateWipBarcode`

**Body params:**

- `Serial` — wip serial
- `Mask` — barcode mask
- `custId`

**Returns:** 0 (fail) or numeric number

**Notes:**

- Any >0 return is a success.

**Sample body:**

```json
{
  "Serial": "E50806012101079262600135",
  "Mask": "",
  "custId": "0"
}
```

### 152. WarehouseWipContainerTransfer ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/WarehouseWipContainerTransfer`

**Body params:**

- `custId` — required and validated
- `serial` — required; board/wip serial
- `sourceContainer` — required; container that board/wip is currently stored
- `targetContrainer` — required; container that board/wip to be transferred to
- `changeTargetContentRule` — if “1”, change the targetContainer’s ContentRule
- `routeStepId` — optional; if no WipMove for the wip required, set it to “0”
- `equipId` — optional; if no WipMove for the wip required, set it to “0”
- `usrId` — required and validated

**Returns:** success/error

**Notes:**

- the source container must be in either packed /partial; hold box will be rejected
- target container must be in a state allowing new board (the box has not yet reach its
- capacity), and its content rule must allow receiving the transferred board. In
- therwise, box count < box capacity and content rule will be validated.
- board transfer only occurs if board is allowed in the box per content rule, and if box
- has not yet reached it capacity .
- This api is intended for warehouse use only, where boards packed in the originally box
- are assumed to have been properly process verified according to route and process
- verification route configuration at the time of original packout.
- The board will be unpacked from the source box; board status will remain unchanged
- After wip transfer, source box status will be changed to partial. Note the original box
- label is likely no longer applied (box content has changed).
- No process verification or material check is performed when repacking board in the
- target box.
- Wipmove will occur if a warehouse wip container transfer step is configured, which is
- reflected by routeStepId and equipId are both supplied.
- Upon transfer, target box will be updated with box count (increased by 1) and its
- status will be changed to Packed if it has been fulled to its capacity.

**Sample body:**

```json
{
  "custId": "0",
  "serial": "E50806012101079262600135",
  "sourceContainer": "",
  "targetContrainer": "",
  "changeTargetContentRule": "",
  "routeStepId": "",
  "equipId": "",
  "usrId": ""
}
```

### 153. WipBirth ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/WipBirth`

**Body params:**

- `custId` — required and validated
- `usrId` — required and validated
- `routeStepId`
- `equipId`
- `assemId` — required
- `serials` — wip serials to be birthed, separated by “|”
- `panelId` — = “0” or CR_Panels.Panel_ID
- `panelSerial` — = “” – wip serial used as panel identifier
- `batchId` — = "0"if none
- `deviationId` — = "0" if none

**Returns:** success/error

**Notes:**

- Panel has been configured in MES
- Route and assembly-route association configured in MES
- Validation includes assembly, panel, serial barcode, etc
- Birth multiple WIPs at once
- If panelId <> 0, serials enter must not exceed panel capacity
- Inserts data in following tables
- dbo.WP_BatchAssemblies
- dbo.WP_Wip
- dbo.WP_Panels
- dbo.WP_AssemblyRouteWip
- dbo.WP_WipRouteSteps
- dbo.QM_DeviationWip

**Sample body:**

```json
{
  "custId": "0",
  "usrId": "",
  "routeStepId": "",
  "equipId": "",
  "assemId": "",
  "serials": "E50806012101079262600135",
  "panelId": "",
  "panelSerial": "E50806012101079262600135",
  "batchId": "",
  "deviationId": ""
}
```

### 154. WipMove ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/WipMove`

**Body params:**

- `usrId` — required and validated
- `wipId` — required
- `routeStepId` — required
- `equipId` — required
- `withPV` — whether PV to be performed
- `opStatusId` — = "1"
- `ioType` — = "0" if in and out
- `newAssemId` — = "0” if not for assembly progression
- `procSingleBoardOnPanel` — = "1”; whether to process single board; should use
- `commonName` — = "" =testerName; if withPV=1, commonName must be valid
- `langId` — “0” is not localized or English – optional

**Returns:** success/error

**Notes:**

- wip must have been created/birthed
- route/routestep/pv configured done and validated in MES
- This function would need be called before linking. Please see LinkWithValidation for
- more details.
- opStatus: 1=Pass, 2=Fail; 3=Abort; 0=Present.
- ioType: 0=In/Out; 1=In; 2=Out
- return PV error: ["RuleText"] + " (" + ["StepText"] + " / " + ["DescrText"] + " -- " +
- ["ParameterText"] + ")"

**Sample body:**

```json
{
  "usrId": "",
  "wipId": "1144749",
  "routeStepId": "",
  "equipId": "",
  "withPV": "",
  "opStatusId": "",
  "ioType": "",
  "newAssemId": "",
  "procSingleBoardOnPanel": "",
  "commonName": "",
  "langId": "0"
}
```

### 155. WipMoveBySerial ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/WipMoveBySerial`

**Body params:**

- `usrId` — required and validated
- `customer` — required
- `division` — required
- `serial` — wip serial
- `factory` — required
- `ma` — required
- `route` — required
- `stepDescr` — required
- `equipment` — =testerName; if withPV=1, commonName must be valid
- `withPV` — whether PV to be performed
- `opStatusId` — = "1"
- `ioType` — = "0"
- `breakoutPanel`
- `newAssemId` — = "0” if not for assembly progression
- `procSingleBoardOnPanel` — = "1”; whether to process single board; should use
- `langId` — “0” is not localized or English – optional

**Returns:** success/error

**Notes:**

- wip must have been created/birthed
- route/routestep/pv configured done and validated in MES
- opStatus: 1=Pass, 2=Fail; 3=Abort; 0=Present.
- ioType: 0=In/Out; 1=In; 2=Out
- return PV error: ["RuleText"] + " (" + ["StepText"] + " / " + ["DescrText"] + " -- " +
- ["ParameterText"] + ")"

**Sample body:**

```json
{
  "usrId": "",
  "customer": "",
  "division": "",
  "serial": "E50806012101079262600135",
  "factory": "",
  "ma": "",
  "route": "",
  "stepDescr": "",
  "equipment": "",
  "withPV": "",
  "opStatusId": "",
  "ioType": "",
  "breakoutPanel": "",
  "newAssemId": "",
  "procSingleBoardOnPanel": "",
  "langId": "0"
}
```

### 156. WipRouteStepInsert ⚠️ *(write / mutating)*

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/WipRouteStepInsert`

**Body params:**

- `wipId`
- `routeStepId`
- `equipId`
- `ioType`
- `opStatusId`
- `panelType` — 0 to 3 numeric value
- `processAsPanel`
- `wipMoveOnly`
- `startUsrId` — = "0”
- `endUsrId` — = "0”

**Returns:** success/error

**Sample body:**

```json
{
  "wipId": "1144749",
  "routeStepId": "",
  "equipId": "",
  "ioType": "",
  "opStatusId": "",
  "panelType": "",
  "processAsPanel": "",
  "wipMoveOnly": "",
  "startUsrId": "",
  "endUsrId": ""
}
```

### 157. WipScanData

`POST https://mypenm0soap03.corp.jabil.org/meswebapi/Wip/WipScanData`

**Body params:**

- `RouteStep`
- `StepInstance`
- `StartDateTime`
- `EndDateTime`
- `LangId`

**Sample body:**

```json
{
  "RouteStep": "",
  "StepInstance": "",
  "StartDateTime": "2025-01-01 00:00:00",
  "EndDateTime": "2025-01-01 00:00:00",
  "LangId": "0"
}
```
