targetScope = 'resourceGroup'

@description('Existing Phase 1 Web App that will be replaced in place.')
param webAppName string = 'skysecure-microsoft-pricing-agent-dev'

resource webApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

// This template changes only the existing Web App runtime configuration. It creates
// no App Service plan, Web App, Service Bus, Key Vault, or monitoring resource.
resource webConfig 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: webApp
  name: 'web'
  properties: {
    linuxFxVersion: 'PYTHON|3.12'
    appCommandLine: 'python main.py'
    alwaysOn: true
    http20Enabled: true
    ftpsState: 'FtpsOnly'
    minTlsVersion: '1.2'
    scmMinTlsVersion: '1.2'
    healthCheckPath: '/health/ready'
  }
}

output webAppResourceId string = webApp.id
output webAppDefaultHostName string = webApp.properties.defaultHostName
output webAppPrincipalId string = webApp.identity.principalId
