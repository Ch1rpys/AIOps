function Initialize-SshIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Directory
    )

    $ErrorActionPreference = 'Stop'
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User
    $privateDirectory = Join-Path $Directory $sid.Value
    New-Item -ItemType Directory -Path $privateDirectory -Force | Out-Null

    # 不同 Windows 账号各用一份私钥，避免沙箱账号的 ACL 导致桌面 SSH 拒绝读取。
    $directoryAcl = New-Object Security.AccessControl.DirectorySecurity
    $directoryAcl.SetOwner($sid)
    $directoryAcl.SetAccessRuleProtection($true, $false)
    $directoryRule = New-Object Security.AccessControl.FileSystemAccessRule(
        $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
    $directoryAcl.AddAccessRule($directoryRule)
    if ($PSVersionTable.PSVersion.Major -le 5) {
        (Get-Item -LiteralPath $privateDirectory).SetAccessControl($directoryAcl)
    } else {
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($privateDirectory), $directoryAcl)
    }

    $destination = Join-Path $privateDirectory 'identity'
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $fileAcl.SetOwner($sid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileRule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
    $fileAcl.AddAccessRule($fileRule)
    # 已存在的副本先收紧权限，再写入，避免更新期间暴露内容。
    if (Test-Path -LiteralPath $destination) {
        if ($PSVersionTable.PSVersion.Major -le 5) {
            (Get-Item -LiteralPath $destination).SetAccessControl($fileAcl)
        } else {
            [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($destination), $fileAcl)
        }
    }
    Copy-Item -LiteralPath $Source -Destination $destination -Force
    if ($PSVersionTable.PSVersion.Major -le 5) {
        (Get-Item -LiteralPath $destination).SetAccessControl($fileAcl)
    } else {
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($destination), $fileAcl)
    }
    $actual = Get-Acl -LiteralPath $destination
    $rules = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($actual.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value -or
        -not $actual.AreAccessRulesProtected -or $rules.Count -ne 1 -or
        $rules[0].IdentityReference.Value -ne $sid.Value -or
        $rules[0].AccessControlType -ne 'Allow') {
        throw 'Cannot secure the per-user SSH identity. Connection was not attempted.'
    }
    return $destination
}
