targetScope = 'resourceGroup'

@description('Name of an existing Azure Container Registry holding the copy image.')
param registryName string

@description('Principal IDs of the job identities that must pull from it.')
param principalIds array

// Microsoft-defined, tenant-independent built-in role ID.
var acrPullRoleDefinitionId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource acrPullAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in principalIds: {
  scope: registry
  name: guid(registry.id, principalId, acrPullRoleDefinitionId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleDefinitionId
    )
  }
}]

output loginServer string = registry.properties.loginServer
