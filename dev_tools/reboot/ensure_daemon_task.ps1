param(
    [string]$TaskName = 'OASDaemon',
    [int]$HeartbeatMinutes = 1
)

# 守护进程必须留在登录用户的交互会话中，不能改成 Session 0 服务。
# 计划任务的失败重启在 TerminateProcess 场景下可能被 Windows 记为“正常完成”，
# 因此额外增加每分钟重复触发器；运行中的任务由 IgnoreNew 保证不会重复启动。
$ErrorActionPreference = 'Stop'
$namespaceUri = 'http://schemas.microsoft.com/windows/2004/02/mit/task'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$xmlText = Export-ScheduledTask -TaskName $TaskName
$document = New-Object System.Xml.XmlDocument
$document.PreserveWhitespace = $true
$document.LoadXml($xmlText)

$namespaces = New-Object System.Xml.XmlNamespaceManager($document.NameTable)
$namespaces.AddNamespace('t', $namespaceUri)

function Set-TaskElementText {
    param(
        [System.Xml.XmlElement]$Parent,
        [string]$Name,
        [string]$Value
    )

    $node = $Parent.SelectSingleNode("t:$Name", $namespaces)
    if ($null -eq $node) {
        $node = $document.CreateElement($Name, $namespaceUri)
        [void]$Parent.AppendChild($node)
    }
    $node.InnerText = $Value
}

$settings = $document.SelectSingleNode('/t:Task/t:Settings', $namespaces)
if ($null -eq $settings) {
    throw "Scheduled task is missing Settings: $TaskName"
}
Set-TaskElementText -Parent $settings -Name 'ExecutionTimeLimit' -Value 'PT0S'
Set-TaskElementText -Parent $settings -Name 'DisallowStartIfOnBatteries' -Value 'false'
Set-TaskElementText -Parent $settings -Name 'StopIfGoingOnBatteries' -Value 'false'
Set-TaskElementText -Parent $settings -Name 'StartWhenAvailable' -Value 'true'

$restart = $settings.SelectSingleNode('t:RestartOnFailure', $namespaces)
if ($null -eq $restart) {
    $restart = $document.CreateElement('RestartOnFailure', $namespaceUri)
    [void]$settings.AppendChild($restart)
}
Set-TaskElementText -Parent $restart -Name 'Count' -Value '10'
Set-TaskElementText -Parent $restart -Name 'Interval' -Value 'PT1M'

$triggers = $document.SelectSingleNode('/t:Task/t:Triggers', $namespaces)
if ($null -eq $triggers) {
    throw "Scheduled task is missing Triggers: $TaskName"
}
$heartbeat = $triggers.SelectSingleNode("t:TimeTrigger[t:Repetition/t:Interval='PT$($HeartbeatMinutes)M']", $namespaces)
if ($null -eq $heartbeat) {
    $heartbeat = $document.CreateElement('TimeTrigger', $namespaceUri)
    $start = $document.CreateElement('StartBoundary', $namespaceUri)
    $start.InnerText = (Get-Date).AddMinutes($HeartbeatMinutes).ToString('s')
    [void]$heartbeat.AppendChild($start)
    $enabled = $document.CreateElement('Enabled', $namespaceUri)
    $enabled.InnerText = 'true'
    [void]$heartbeat.AppendChild($enabled)
    $repetition = $document.CreateElement('Repetition', $namespaceUri)
    $interval = $document.CreateElement('Interval', $namespaceUri)
    $interval.InnerText = "PT$($HeartbeatMinutes)M"
    [void]$repetition.AppendChild($interval)
    $stopAtEnd = $document.CreateElement('StopAtDurationEnd', $namespaceUri)
    $stopAtEnd.InnerText = 'false'
    [void]$repetition.AppendChild($stopAtEnd)
    [void]$heartbeat.AppendChild($repetition)
    [void]$triggers.AppendChild($heartbeat)
}

Register-ScheduledTask -TaskName $TaskName -Xml $document.OuterXml -Force | Out-Null
Write-Output "Configured $TaskName heartbeat every $HeartbeatMinutes minute(s)"
