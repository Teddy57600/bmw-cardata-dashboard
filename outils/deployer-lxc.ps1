<#
.SYNOPSIS
    Déploie le tableau de bord (commande « stream ») dans Docker, sur un conteneur LXC Proxmox.

.DESCRIPTION
    Copie le code par SSH sur l'hôte Proxmox, le pousse dans le LXC (pct push), puis lance
    « docker compose up -d --build » dans /opt/bmw-cardata.

    Sans -AvecDonnees, seul le code est mis à jour : les données du serveur sont conservées.
    Avec -AvecDonnees, la configuration, les jetons, la base de l'historique et les autres
    données personnelles de ce PC sont copiés (les anciennes données du serveur sont mises de
    côté dans data.avant-<date>). Une seule connexion de streaming est possible par compte BMW :
    arrêtez « stream » sur ce PC avant, et ne le relancez pas ensuite.

.EXAMPLE
    .\outils\deployer-lxc.ps1 -ProxmoxHost 192.168.1.10 -ContainerId 102 -BindIp 192.168.1.20 -Port 8766 -AvecDonnees
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9_.@:-]+$')][string] $ProxmoxHost,
    [Parameter(Mandatory)][ValidateRange(100, 999999999)][int] $ContainerId,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F.:]+$')][string] $BindIp,
    [ValidateRange(1, 65535)][int] $Port = 8765,
    [string] $IdentityFile,
    [ValidatePattern('^/[a-zA-Z0-9_./-]+$')][string] $Destination = '/opt/bmw-cardata',
    [switch] $AvecDonnees
)
$ErrorActionPreference = 'Stop'
$proj = Split-Path $PSScriptRoot -Parent
$remote = if ($ProxmoxHost.Contains('@')) { $ProxmoxHost } else { "root@$ProxmoxHost" }
$ssh = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15')
if ($IdentityFile) { $ssh = @('-i', $IdentityFile) + $ssh }

$appFiles = 'bmw_cardata.py', 'dashboard_template.html', 'catalogue_bmw.json', 'requirements.txt',
            'Dockerfile', '.dockerignore', 'compose.yaml'
$dataFiles = 'config.json', 'tokens.json', 'historique.db', 'derniere_lecture.json', 'historique_stream.jsonl',
             'historique_lectures.jsonl', 'profil_vehicule.json', 'voiture.png', 'voiture.jpg'

if ($AvecDonnees) {
    $running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'bmw_cardata\.py.*\bstream\b' }
    if ($running) { throw "« stream » tourne encore sur ce PC (PID $($running.ProcessId -join ', ')) : arrêtez-le avant de copier les données." }
    foreach ($f in 'config.json', 'tokens.json') {
        if (-not (Test-Path (Join-Path $proj $f))) { throw "$f introuvable : faites d'abord « python bmw_cardata.py login »." }
    }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("bmw-deploy-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$stage = Join-Path $work 'stage'
$archive = Join-Path $work 'bmw-cardata.tgz'
$tag = Split-Path $work -Leaf
try {
    New-Item -ItemType Directory -Force (Join-Path $stage 'app'), (Join-Path $stage 'data') | Out-Null
    foreach ($f in $appFiles) { Copy-Item (Join-Path $proj $f) (Join-Path $stage 'app') }
    # script d'installation avec des fins de ligne Unix
    $sh = (Get-Content -Raw (Join-Path $PSScriptRoot 'installer-lxc.sh')) -replace "`r`n", "`n"
    [IO.File]::WriteAllText((Join-Path $stage 'installer-lxc.sh'), $sh)
    if ($AvecDonnees) {
        foreach ($f in $dataFiles) {
            $p = Join-Path $proj $f
            if (Test-Path $p) { Copy-Item $p (Join-Path $stage 'data') }
        }
    }
    tar -czf $archive -C $stage .
    if ($LASTEXITCODE) { throw "Création de l'archive impossible." }

    & scp @ssh $archive "${remote}:/root/$tag.tgz"
    if ($LASTEXITCODE) { throw "Copie vers $ProxmoxHost impossible." }
    $withData = if ($AvecDonnees) { 1 } else { 0 }
    $cmd = "set -e; trap 'rm -f /root/$tag.tgz' EXIT; " +
           "pct push $ContainerId /root/$tag.tgz /tmp/$tag.tgz; " +
           "pct exec $ContainerId -- bash -c 'set -e; trap ""rm -f /tmp/$tag.tgz"" EXIT; mkdir -p /tmp/$tag; " +
           "tar -xzf /tmp/$tag.tgz -C /tmp/$tag; bash /tmp/$tag/installer-lxc.sh /tmp/$tag $Destination $BindIp $Port $withData'"
    & ssh @ssh $remote $cmd
    if ($LASTEXITCODE) { throw "Installation sur le LXC $ContainerId en échec (code $LASTEXITCODE)." }
    Write-Host "`nTableau de bord : http://${BindIp}:$Port/" -ForegroundColor Green
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
