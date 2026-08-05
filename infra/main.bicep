targetScope = 'resourceGroup'

@description('Azure region for the copy service. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Prefix for every resource name. 3-20 lowercase letters, numbers, or hyphens.')
@minLength(3)
@maxLength(20)
param baseName string = 'file-copy'

@description('Private address space for the dedicated virtual network. Must not overlap networks you already connect to.')
param vnetAddressPrefix string = '10.240.0.0/16'

@description('Container Apps infrastructure subnet. Must be /27 or larger and inside the virtual network.')
param containerAppsSubnetPrefix string = '10.240.0.0/27'

@description('Published application image, pinned by digest. Override to use a copy imported into your own registry.')
param containerImage string

@description('Optional. Resource ID of a private Azure Container Registry holding the image. Leave empty when the image is publicly pullable. Container Apps validates the image at job-creation time, so a private registry will not work without this.')
param containerRegistryResourceId string = ''

@description('One entry per copy job. See jobs/example.jsonc for the field reference.')
param jobs array

var usePrivateRegistry = !empty(containerRegistryResourceId)
var registryIdSegments = split(containerRegistryResourceId, '/')
var registrySubscriptionId = usePrivateRegistry ? registryIdSegments[2] : subscription().subscriptionId
var registryResourceGroup = usePrivateRegistry ? registryIdSegments[4] : resourceGroup().name
var registryName = usePrivateRegistry ? last(registryIdSegments) : 'unused'

// Taken from the image reference rather than the conditional module's output:
// referencing that output would evaluate even on the public-registry path,
// where the module is never deployed.
var registryLoginServer = usePrivateRegistry ? split(containerImage, '/')[0] : ''

var tags = {
  workload: 'azure-sharepoint-copy'
}

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, baseName)
var keyVaultName = take('${baseName}-${suffix}', 24)
var logWorkspaceName = '${baseName}-logs'
var environmentName = '${baseName}-environment'
var vnetName = '${baseName}-vnet'
var subnetName = 'container-apps'

// 31 February never occurs, so a job carrying this expression is deployed but
// dormant. copyctl.py enable installs the real schedule after the operator has
// stored a credential and reviewed a dry run.
var parkedCronExpression = '0 0 31 2 *'

// Microsoft-defined, tenant-independent built-in role ID.
var keyVaultSecretsUserRoleDefinitionId = '4633458b-17de-408a-b874-0445c86b69e6'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    // Purge protection is deliberately omitted. It cannot be switched off once
    // enabled, and combined with this deterministic vault name it would make a
    // teardown-then-redeploy cycle fail until the retention window expired.
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
  tags: tags
}

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  properties: {
    retentionInDays: 90
    sku: {
      name: 'PerGB2018'
    }
  }
  tags: tags
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: containerAppsSubnetPrefix
          delegations: [
            {
              name: 'container-apps-environment'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
          serviceEndpoints: [
            {
              service: 'Microsoft.Storage.Global'
            }
          ]
        }
      }
    ]
  }
  tags: tags
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: virtualNetwork
  name: subnetName
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWorkspace.properties.customerId
        sharedKey: logWorkspace.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
  tags: tags
}

resource jobIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for job in jobs: {
  name: '${baseName}-${job.name}-identity'
  location: location
  tags: union(tags, {
    copyJob: job.name
  })
}]

// A non-credential placeholder lets the job's secret reference resolve before
// copyctl.py set-secret stores the real value. The real secret never passes
// through this template or its deployment history.
resource placeholderSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for job in jobs: {
  parent: keyVault
  name: 'sharepoint-${job.name}'
  properties: {
    value: 'secret-not-set'
    contentType: 'Placeholder; replace with copyctl.py set-secret'
  }
  tags: union(tags, {
    copyJob: job.name
  })
}]

resource secretReadAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (job, index) in jobs: {
  scope: placeholderSecrets[index]
  name: guid(placeholderSecrets[index].id, jobIdentities[index].id, keyVaultSecretsUserRoleDefinitionId)
  properties: {
    principalId: jobIdentities[index].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleDefinitionId
    )
  }
}]

// Container Apps resolves the image while creating the job, so AcrPull has to
// be in place before the jobs below are created.
module registryAccess 'registry-access.bicep' = {
  name: 'registry-access-${uniqueString(containerRegistryResourceId)}'
  scope: resourceGroup(registrySubscriptionId, registryResourceGroup)
  params: {
    enabled: usePrivateRegistry
    registryName: registryName
    principalIds: [for (job, index) in jobs: jobIdentities[index].properties.principalId]
  }
}

module sourceAccess 'source-access.bicep' = [for (job, index) in jobs: {
  name: 'source-access-${job.name}-${uniqueString(jobIdentities[index].id, job.source.storageAccount)}'
  scope: resourceGroup(job.source.subscriptionId, job.source.resourceGroup)
  params: {
    principalId: jobIdentities[index].properties.principalId
    sourceType: job.source.type
    storageAccountName: job.source.storageAccount
    containerOrShareName: job.source.containerOrShare
  }
}]

resource copyJobs 'Microsoft.App/jobs@2024-03-01' = [for (job, index) in jobs: {
  name: '${baseName}-${job.name}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${jobIdentities[index].id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: parkedCronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaRetryLimit: 1
      replicaTimeout: job.copy.timeoutMinutes * 60
      registries: usePrivateRegistry ? [
        {
          server: registryLoginServer
          identity: jobIdentities[index].id
        }
      ] : []
      secrets: [
        {
          name: 'sharepoint-client-secret'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/sharepoint-${job.name}'
          identity: jobIdentities[index].id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'copy'
          image: containerImage
          // The full job configuration is visible here on purpose. Reading the
          // job in the portal or with `az containerapp job show` is a complete
          // and current record of what this job will copy and where.
          env: [
            {
              name: 'COPY_JOB_NAME'
              value: job.name
            }
            {
              name: 'SOURCE_TYPE'
              value: job.source.type
            }
            {
              // Not read by the container. Carried so the deployed job is a
              // complete record and copyctl.py pull can rebuild the job file.
              name: 'SOURCE_SUBSCRIPTION_ID'
              value: job.source.subscriptionId
            }
            {
              name: 'SOURCE_RESOURCE_GROUP'
              value: job.source.resourceGroup
            }
            {
              name: 'SOURCE_STORAGE_ACCOUNT'
              value: job.source.storageAccount
            }
            {
              name: 'SOURCE_CONTAINER_OR_SHARE'
              value: job.source.containerOrShare
            }
            {
              name: 'SOURCE_PATH'
              value: job.source.path
            }
            {
              name: 'SOURCE_INCLUDE_PATHS'
              value: string(job.source.includePaths)
            }
            {
              name: 'SOURCE_MODIFIED_ON_OR_AFTER'
              value: job.source.modifiedOnOrAfter
            }
            {
              name: 'DEST_TENANT_ID'
              value: job.destination.tenantId
            }
            {
              name: 'DEST_CLIENT_ID'
              value: job.destination.clientId
            }
            {
              name: 'DEST_SITE_URL'
              value: job.destination.siteUrl
            }
            {
              name: 'DEST_LIBRARY'
              value: job.destination.library
            }
            {
              name: 'DEST_PATH'
              value: job.destination.path
            }
            {
              name: 'COPY_EXISTING_FILES'
              value: job.copy.existingFiles
            }
            {
              name: 'COPY_EMPTY_FOLDERS'
              value: job.copy.emptyFolders
            }
            {
              // ARM renders a raw boolean as 'True'/'False'; the runtime
              // requires lowercase, so the casing is fixed here.
              name: 'COPY_DRY_RUN'
              value: job.copy.dryRun ? 'true' : 'false'
            }
            {
              // The schedule the operator intends to activate. The trigger
              // stays parked until copyctl.py enable installs this value.
              name: 'COPY_SCHEDULE_UTC'
              value: job.copy.scheduleUtc
            }
            {
              name: 'AZURE_MANAGED_IDENTITY_CLIENT_ID'
              value: jobIdentities[index].properties.clientId
            }
            {
              name: 'RCLONE_CONFIG_DESTINATION_CLIENT_SECRET'
              secretRef: 'sharepoint-client-secret'
            }
            {
              name: 'RCLONE_TRANSFERS'
              value: '4'
            }
            {
              name: 'RCLONE_CHECKERS'
              value: '8'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
        }
      ]
    }
  }
  tags: union(tags, {
    copyJob: job.name
  })
  dependsOn: [
    secretReadAssignments[index]
    sourceAccess[index]
    registryAccess
  ]
}]

output keyVaultName string = keyVault.name
output containerAppsSubnetId string = containerAppsSubnet.id
output copyJobs array = [for (job, index) in jobs: {
  name: job.name
  jobResourceName: copyJobs[index].name
  identityPrincipalId: jobIdentities[index].properties.principalId
  secretName: 'sharepoint-${job.name}'
}]
