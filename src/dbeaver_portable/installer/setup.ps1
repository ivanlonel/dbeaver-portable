#Requires -Version 5.1
<#
.SYNOPSIS
    Installs, updates, resets or removes the DBeaver of this package, without admin rights.

.DESCRIPTION
    Run through instalar.cmd (from the package) or setup.cmd (from the installed copy, which is what
    the Windows "Installed apps" entry calls). Without an action it shows a menu.

    install    Mirrors the app into InstallDir, updates the packaged driver files, creates the DBeaver
               workspace or merges into an existing one (library entries of the packaged drivers in
               drivers.xml, default files the user lacks, and packaged connections never offered
               before), shortcuts and the uninstall entry.
    reset      Restores the packaged default preferences; connections, scripts and drivers stay.
    uninstall  Removes the app, the driver files, the shortcuts and the uninstall entry and, if the
               user agrees, the whole DBeaverData folder.

    State kept between runs lives in DataDir\portable-install.json. User-facing messages are in
    Brazilian Portuguese.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('menu', 'install', 'reset', 'uninstall')]
    [string] $Action = 'menu',
    [string] $InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\DBeaver'),
    [string] $DataDir = (Join-Path $env:APPDATA 'DBeaverData'),
    [string] $DesktopDir = [Environment]::GetFolderPath('Desktop'),
    [string] $StartMenuDir = [Environment]::GetFolderPath('Programs'),
    [string] $RegistryKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DBeaverPortable',
    [switch] $NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Web.Extensions
$Json = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$Json.MaxJsonLength = [int]::MaxValue

$PortableDir = $PSScriptRoot
$AppSource = Split-Path -Parent $PortableDir
$PackageDrivers = Join-Path (Split-Path -Parent $AppSource) 'DBeaverData\drivers'
$Workspace = Join-Path $DataDir 'workspace6'
$DriversDir = Join-Path $DataDir 'drivers'
$StateFile = Join-Path $DataDir 'portable-install.json'
$DriversXml = Join-Path $Workspace '.metadata\.config\drivers.xml'
$DataSourcesJson = Join-Path $Workspace 'General\.dbeaver\data-sources.json'
$ShortcutName = 'DBeaver.lnk'

function Get-FullPath([string] $Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

$FromPackage = (Test-Path -LiteralPath $PackageDrivers) -and
    ((Get-FullPath $AppSource) -ne (Get-FullPath $InstallDir))

function Write-Step([string] $Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Confirm-Choice([string] $Question, [bool] $Default) {
    if ($NonInteractive) { return $Default }
    $hint = if ($Default) { 'S/n' } else { 's/N' }
    $answer = (Read-Host "$Question [$hint]").Trim()
    if (-not $answer) { return $Default }
    return $answer.ToLower().StartsWith('s')
}

function Remove-Path([string] $Path) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

function New-Folder([string] $Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Copy-File([string] $Source, [string] $Destination) {
    New-Folder (Split-Path -Parent $Destination)
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Invoke-Robocopy([string] $Source, [string] $Destination, [string[]] $Options) {
    # Exit codes 0-7 report success (copied, skipped, extra files); 8 and above are failures.
    & robocopy.exe $Source $Destination @Options /NFL /NDL /NJH /NJS /NP /R:2 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Falha ao copiar $Source para $Destination (robocopy retornou $LASTEXITCODE)."
    }
}

function Remove-EmptyFolders([string] $Root) {
    if (-not (Test-Path -LiteralPath $Root)) { return }
    Get-ChildItem -LiteralPath $Root -Recurse -Directory |
        Sort-Object { $_.FullName.Length } -Descending |
        Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}

function Write-Utf8([string] $Path, [string] $Text) {
    New-Folder (Split-Path -Parent $Path)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false))
}

function Read-Json([string] $Path) {
    return $Json.DeserializeObject([IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8))
}

function New-Map {
    return New-Object 'System.Collections.Generic.Dictionary[string,object]'
}

function Get-Entry($Map, [string] $Key) {
    if ($null -ne $Map -and $Key -and $Map.ContainsKey($Key)) { return $Map[$Key] }
    return $null
}

function Set-Entry($Map, [string] $Key, $Value) {
    # Function results reach callers wrapped in PSObject, which JavaScriptSerializer would walk
    # as data (and choke on); always store the underlying .NET object.
    $Map[$Key] = $Value.psobject.BaseObject
}

function ConvertTo-JsonText($Value) {
    return $Json.Serialize($Value.psobject.BaseObject)
}

function Get-Section($Map, [string] $Key) {
    $section = Get-Entry $Map $Key
    if ($null -eq $section) {
        $section = New-Map
        Set-Entry $Map $Key $section
    }
    return $section
}

function Get-List($Map, [string] $Key) {
    $value = Get-Entry $Map $Key
    if ($null -eq $value) { return , @() }
    return , @($value)
}

function Get-State {
    if (Test-Path -LiteralPath $StateFile) { return Read-Json $StateFile }
    return New-Map
}

function Save-State($State) {
    Write-Utf8 $StateFile (ConvertTo-JsonText $State)
}

function Get-PackageVersion {
    $line = Get-Content -LiteralPath (Join-Path $AppSource '.eclipseproduct') |
        Where-Object { $_ -like 'version=*' } |
        Select-Object -First 1
    if ($line) { return $line.Substring('version='.Length).Trim() }
    return '?'
}

function Assert-DBeaverClosed {
    $install = (Get-FullPath $InstallDir) + '\'
    $running = @(Get-Process -Name 'dbeaver', 'dbeaverc' -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.StartsWith($install, [StringComparison]::OrdinalIgnoreCase) }
            catch { $false }
        })
    if ($running.Count -gt 0) { throw 'O DBeaver está aberto. Feche-o e tente novamente.' }
}

function Sync-Drivers($State, [bool] $Legacy) {
    $manifest = @(Get-Content -LiteralPath (Join-Path $PortableDir 'drivers-manifest.txt') -Encoding UTF8 |
            Where-Object { $_ })
    if ($Legacy) {
        # The previous kind of package mirrored this folder on every install; do so one last time.
        Invoke-Robocopy $PackageDrivers $DriversDir @('/MIR')
    } else {
        # Copy without purging: driver files the user downloaded for other databases stay. Only the
        # files a previous package installed and this one no longer ships are removed.
        Invoke-Robocopy $PackageDrivers $DriversDir @('/E')
        foreach ($old in (Get-List $State 'driverFiles')) {
            if ($manifest -notcontains $old) { Remove-Path (Join-Path $DriversDir $old) }
        }
        Remove-EmptyFolders $DriversDir
    }
    Set-Entry $State 'driverFiles' ([string[]] $manifest)
}

function Initialize-Workspace {
    Invoke-Robocopy (Join-Path $PortableDir 'defaults') $DataDir @('/E')
    Copy-File (Join-Path $PortableDir 'drivers.xml') $DriversXml
}

function Save-Xml([Xml.XmlDocument] $Document, [string] $Path) {
    $settings = New-Object Xml.XmlWriterSettings
    $settings.Indent = $true
    $settings.IndentChars = "`t"
    $settings.Encoding = New-Object Text.UTF8Encoding $false
    $writer = [Xml.XmlWriter]::Create($Path, $settings)
    try { $Document.Save($writer) } finally { $writer.Dispose() }
}

function Merge-DriversXml {
    $packagedPath = Join-Path $PortableDir 'drivers.xml'
    if (-not (Test-Path -LiteralPath $DriversXml)) {
        Copy-File $packagedPath $DriversXml
        return
    }
    $packaged = New-Object Xml.XmlDocument
    $packaged.Load($packagedPath)
    $user = New-Object Xml.XmlDocument
    $user.Load($DriversXml)
    foreach ($provider in @($packaged.DocumentElement.SelectNodes('provider'))) {
        $userProvider = $user.DocumentElement.SelectSingleNode("provider[@id='$($provider.GetAttribute('id'))']")
        if ($null -eq $userProvider) {
            [void] $user.DocumentElement.AppendChild($user.ImportNode($provider, $true))
            continue
        }
        foreach ($driver in @($provider.SelectNodes('driver'))) {
            $userDriver = $userProvider.SelectSingleNode("driver[@id='$($driver.GetAttribute('id'))']")
            if ($null -eq $userDriver) {
                [void] $userProvider.AppendChild($user.ImportNode($driver, $true))
                continue
            }
            # Packaged libraries replace the plugin-defined ones; libraries the user added stay
            # (DBeaver treats a library without custom="false" as user-added).
            foreach ($library in @($userDriver.SelectNodes('library'))) {
                if ($library.GetAttribute('custom') -eq 'false') { [void] $userDriver.RemoveChild($library) }
            }
            foreach ($library in @($driver.SelectNodes('library'))) {
                [void] $userDriver.AppendChild($user.ImportNode($library, $true))
            }
        }
    }
    Save-Xml $user $DriversXml
}

function Add-Folders($Packaged, $User, [string] $Folder) {
    $packagedFolders = Get-Entry $Packaged 'folders'
    while ($Folder) {
        $folders = Get-Section $User 'folders'
        $definition = Get-Entry $packagedFolders $Folder
        if ($folders.ContainsKey($Folder) -or $null -eq $definition) { return }
        Set-Entry $folders $Folder $definition
        $Folder = Get-Entry $definition 'parent'
    }
}

function Add-ConnectionType($Packaged, $User, [string] $Type) {
    $definition = Get-Entry (Get-Entry $Packaged 'connection-types') $Type
    if ($null -eq $definition) { return }
    $types = Get-Section $User 'connection-types'
    if (-not $types.ContainsKey($Type)) { Set-Entry $types $Type $definition }
}

function Merge-DataSources($State) {
    $packagedPath = Join-Path $PortableDir 'data-sources.json'
    if (-not (Test-Path -LiteralPath $packagedPath)) { return }
    $packaged = Read-Json $packagedPath
    $packagedConnections = Get-Section $packaged 'connections'
    $offered = Get-List $State 'offeredDataSources'
    $user = if (Test-Path -LiteralPath $DataSourcesJson) { Read-Json $DataSourcesJson } else { New-Map }
    $connections = Get-Section $user 'connections'
    $added = 0
    foreach ($id in @($packagedConnections.Keys)) {
        # Never offer a connection twice, so the ones a user deleted stay deleted.
        if ($offered -contains $id -or $connections.ContainsKey($id)) { continue }
        $connection = $packagedConnections[$id]
        Set-Entry $connections $id $connection
        Add-Folders $packaged $user (Get-Entry $connection 'folder')
        Add-ConnectionType $packaged $user (Get-Entry (Get-Entry $connection 'configuration') 'type')
        $added++
    }
    if ($added -gt 0) {
        Write-Step "Adicionando $added conexão(ões) do pacote"
        Write-Utf8 $DataSourcesJson (ConvertTo-JsonText $user)
    }
    $everOffered = [string[]] @(@($offered) + @($packagedConnections.Keys) | Select-Object -Unique)
    Set-Entry $State 'offeredDataSources' $everOffered
}

function Set-Shortcuts {
    $shell = New-Object -ComObject WScript.Shell
    $exe = Join-Path $InstallDir 'dbeaver.exe'
    foreach ($dir in @($DesktopDir, $StartMenuDir)) {
        New-Folder $dir
        $link = $shell.CreateShortcut((Join-Path $dir $ShortcutName))
        $link.TargetPath = $exe
        $link.WorkingDirectory = $InstallDir
        $link.IconLocation = "$exe,0"
        $link.Save()
    }
}

function Register-Uninstaller([string] $Version) {
    $setup = Join-Path (Join-Path $InstallDir (Split-Path -Leaf $PortableDir)) 'setup.cmd'
    $bytes = (Get-ChildItem -LiteralPath $InstallDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
    if (-not (Test-Path -Path $RegistryKey)) { New-Item -Path $RegistryKey -Force | Out-Null }
    $strings = [ordered] @{
        DisplayName     = 'DBeaver Community'
        DisplayVersion  = $Version
        InstallLocation = $InstallDir
        DisplayIcon     = (Join-Path $InstallDir 'dbeaver.exe') + ',0'
        UninstallString = "`"$setup`" uninstall"
        ModifyPath      = "`"$setup`""
    }
    foreach ($name in $strings.Keys) {
        New-ItemProperty -Path $RegistryKey -Name $name -Value $strings[$name] -PropertyType String -Force | Out-Null
    }
    New-ItemProperty -Path $RegistryKey -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $RegistryKey -Name EstimatedSize -Value ([int] ($bytes / 1KB)) -PropertyType DWord -Force |
        Out-Null
}

function Install-Package {
    if (-not $FromPackage) { throw 'Para instalar ou atualizar, execute o instalar.cmd da pasta do pacote.' }
    Assert-DBeaverClosed
    $version = Get-PackageVersion
    $state = Get-State
    $legacy = (Test-Path -LiteralPath (Join-Path $InstallDir 'dbeaver.exe')) -and
        -not (Test-Path -LiteralPath $StateFile)
    Write-Step "Copiando o DBeaver $version para $InstallDir"
    Invoke-Robocopy $AppSource $InstallDir @('/MIR')
    Write-Step "Copiando os drivers para $DriversDir"
    Sync-Drivers $state $legacy
    if (Test-Path -LiteralPath $Workspace) {
        Write-Step 'Atualizando os drivers do pacote na configuração existente'
        Merge-DriversXml
        # Fill in default files the user lacks (such as product-config.json, without which DBeaver
        # asks its first-run questions again) without touching any existing setting.
        Invoke-Robocopy (Join-Path $PortableDir 'defaults') $DataDir @('/E', '/XC', '/XN', '/XO')
    } else {
        Write-Step 'Criando a configuração padrão'
        Initialize-Workspace
    }
    Merge-DataSources $state
    Write-Step 'Criando os atalhos e a entrada em "Aplicativos instalados"'
    Set-Shortcuts
    Register-Uninstaller $version
    Set-Entry $state 'version' ([string] $version)
    Save-State $state
    Write-Host "DBeaver $version instalado." -ForegroundColor Green
}

function Reset-Preferences {
    Assert-DBeaverClosed
    if (-not (Confirm-Choice 'Restaurar as preferências padrão? Conexões, scripts e drivers são mantidos.' $true)) {
        return
    }
    $settings = Join-Path $Workspace '.metadata\.plugins\org.eclipse.core.runtime\.settings'
    if (Test-Path -LiteralPath $settings) {
        Get-ChildItem -LiteralPath $settings -Filter '*.prefs' | Remove-Item -Force
    }
    Remove-Path (Join-Path $Workspace '.metadata\.plugins\org.eclipse.e4.workbench\workbench.xmi')
    Invoke-Robocopy (Join-Path $PortableDir 'defaults') $DataDir @('/E')
    Write-Host 'Preferências padrão restauradas.' -ForegroundColor Green
}

function Uninstall-Package {
    Assert-DBeaverClosed
    if (-not (Confirm-Choice 'Desinstalar o DBeaver?' $true)) { return }
    Set-Location -LiteralPath ([IO.Path]::GetTempPath())
    Write-Step 'Removendo o DBeaver, os drivers e os atalhos'
    Remove-Path $InstallDir
    Remove-Path $DriversDir
    foreach ($dir in @($DesktopDir, $StartMenuDir)) { Remove-Path (Join-Path $dir $ShortcutName) }
    if (Test-Path -Path $RegistryKey) { Remove-Item -Path $RegistryKey -Recurse -Force }
    if (Confirm-Choice 'Remover também suas configurações e conexões (pasta DBeaverData)?' $false) {
        Remove-Path $DataDir
    } elseif (Test-Path -LiteralPath $StateFile) {
        $state = Get-State
        [void] $state.Remove('driverFiles')
        Save-State $state
    }
    Write-Host 'DBeaver desinstalado.' -ForegroundColor Green
}

function Select-Action {
    $labels = @{
        install   = 'Instalar ou atualizar'
        reset     = 'Restaurar as preferências padrão (mantém conexões, scripts e drivers)'
        uninstall = 'Desinstalar'
    }
    $choices = [ordered] @{}
    if ($FromPackage) { $choices['1'] = 'install' }
    $choices['2'] = 'reset'
    $choices['3'] = 'uninstall'
    Write-Host ''
    Write-Host "DBeaver Community $(Get-PackageVersion)" -ForegroundColor Cyan
    Write-Host ''
    foreach ($key in $choices.Keys) { Write-Host "  $key) $($labels[$choices[$key]])" }
    Write-Host '  0) Sair'
    $default = if ($FromPackage) { '1' } else { '0' }
    $answer = (Read-Host "Escolha uma opção [$default]").Trim()
    if (-not $answer) { $answer = $default }
    if ($choices.Contains($answer)) { return $choices[$answer] }
    return $null
}

$exitCode = 0
try {
    $selected = if ($Action -eq 'menu') { Select-Action } else { $Action }
    switch ($selected) {
        'install' { Install-Package }
        'reset' { Reset-Preferences }
        'uninstall' { Uninstall-Package }
    }
} catch {
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
if (-not $NonInteractive) { [void] (Read-Host 'Pressione Enter para sair') }
exit $exitCode
