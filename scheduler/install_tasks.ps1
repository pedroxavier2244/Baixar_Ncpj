# Run as Administrator:
# powershell -ExecutionPolicy Bypass -File scheduler\install_tasks.ps1

param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe  = "$ProjectDir\.venv\Scripts\python.exe"
)

$EnqueueScript = "$ProjectDir\enqueue_job.py"
$WorkerBat     = "$ProjectDir\start_worker.bat"
$ApiBat        = "$ProjectDir\start_api.bat"

# Task 1: Check for new month daily at 06:00
$action1  = New-ScheduledTaskAction -Execute $PythonExe -Argument $EnqueueScript -WorkingDirectory $ProjectDir
$trigger1 = New-ScheduledTaskTrigger -Daily -At "06:00"
$settings1 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "ETL_CNPJ_Enqueue" `
    -Action $action1 -Trigger $trigger1 -Settings $settings1 `
    -Description "Verifica novo mes da Receita Federal e enfileira job" `
    -RunLevel Highest -Force

Write-Host "OK: ETL_CNPJ_Enqueue criada"

# Task 2: Worker — start at system boot, keep running
$action2  = New-ScheduledTaskAction -Execute $WorkerBat -WorkingDirectory $ProjectDir
$trigger2 = New-ScheduledTaskTrigger -AtStartup
$settings2 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 7) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "ETL_CNPJ_Worker" `
    -Action $action2 -Trigger $trigger2 -Settings $settings2 `
    -Description "Worker ETL CNPJ — roda continuamente" `
    -RunLevel Highest -Force

Write-Host "OK: ETL_CNPJ_Worker criada"

# Task 3: API — start at system boot
$action3  = New-ScheduledTaskAction -Execute $ApiBat -WorkingDirectory $ProjectDir
$trigger3 = New-ScheduledTaskTrigger -AtStartup
$settings3 = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "ETL_CNPJ_API" `
    -Action $action3 -Trigger $trigger3 -Settings $settings3 `
    -Description "API FastAPI de consulta CNPJ" `
    -RunLevel Highest -Force

Write-Host "OK: ETL_CNPJ_API criada"
Write-Host ""
Write-Host "Tarefas instaladas. Reinicie o servidor ou inicie manualmente:"
Write-Host "  Start-ScheduledTask -TaskName ETL_CNPJ_Worker"
Write-Host "  Start-ScheduledTask -TaskName ETL_CNPJ_API"
