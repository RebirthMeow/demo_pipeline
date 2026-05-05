$ErrorActionPreference = 'Stop'

Write-Host "========================================================="
Write-Host " Testing Parallel Rendering (3 Workers)"
Write-Host "========================================================="
Write-Host "Get your Task Manager ready!"
Write-Host "Waiting 3 seconds..."
Start-Sleep -Seconds 3

# Clear the render state so it's forced to re-render
$stateFile = "a full jka install\game_directory\Jedi Academy\GameData\mme\render_state.json"
if (Test-Path $stateFile) {
    Remove-Item $stateFile -Force
}

# Run the python script directly with 3 workers, limited to 3 clips to keep it brief
python python\predict\render_clips.py --workers 3 --limit 3

Write-Host "========================================================="
Write-Host " Test Complete."
Write-Host "========================================================="
