# Creates (or updates) all labels used by recipebot. Requires: gh auth login.
# Usage: .\scripts\create-labels.ps1 [-Repo CojocaruCristianV/recipes]
param([string]$Repo = "CojocaruCristianV/recipes")

$labels = @(
    @{ n = "recipe";       c = "5319e7"; d = "Recipe link from the iOS Shortcut" },
    @{ n = "queued";       c = "fbca04"; d = "Waiting to be processed" },
    @{ n = "retry";        c = "f9d0c4"; d = "Add to re-queue a failed or reviewed item" },
    @{ n = "processing";   c = "1d76db"; d = "Being processed" },
    @{ n = "done";         c = "0e8a16"; d = "Added to recipes.json" },
    @{ n = "needs-review"; c = "d93f0b"; d = "Added, but contains TBD items" },
    @{ n = "failed";       c = "b60205"; d = "Failed after retry; see comment" },
    @{ n = "duplicate";    c = "cfd3d7"; d = "Already in the library" },
    @{ n = "not-recipe";   c = "eeeeee"; d = "No ingredients and no method" },
    @{ n = "needs-local";  c = "c5def5"; d = "Blocked from GitHub IPs; for the self-hosted runner" }
)

foreach ($l in $labels) {
    gh label create $l.n --repo $Repo --color $l.c --description $l.d --force
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create label $($l.n)"; exit 1 }
}
Write-Host "Labels ready on $Repo"
