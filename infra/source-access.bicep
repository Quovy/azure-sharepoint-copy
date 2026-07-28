targetScope = 'resourceGroup'

@allowed([
  'azure_files'
  'adls_gen2'
])
param sourceType string

param principalId string
param storageAccountName string
param containerOrShareName string

// Microsoft-defined, tenant-independent built-in role IDs.
var storageFileDataPrivilegedReaderRoleDefinitionId = 'b8eda974-7b85-4f76-af95-65846b26df6d'
var storageBlobDataReaderRoleDefinitionId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = if (sourceType == 'adls_gen2') {
  parent: storageAccount
  name: 'default'
}

resource sourceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = if (sourceType == 'adls_gen2') {
  parent: blobService
  name: containerOrShareName
}

// Azure Files OAuth-over-REST uses backup semantics: this role is read-only but
// applies across the storage account and bypasses per-file NTFS ACLs. Use a
// source-dedicated storage account if account-wide read access is too broad.
resource azureFilesReadAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (sourceType == 'azure_files') {
  scope: storageAccount
  name: guid(storageAccount.id, principalId, storageFileDataPrivilegedReaderRoleDefinitionId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageFileDataPrivilegedReaderRoleDefinitionId
    )
  }
}

// ADLS Gen2 keeps a narrower container-level read boundary.
resource adlsReadAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (sourceType == 'adls_gen2') {
  scope: sourceContainer
  name: guid(sourceContainer.id, principalId, storageBlobDataReaderRoleDefinitionId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataReaderRoleDefinitionId
    )
  }
}
