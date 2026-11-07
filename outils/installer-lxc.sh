#!/usr/bin/env bash
# Installe ou met à jour le conteneur Docker bmw-cardata sur le serveur.
# Appelé par outils/deployer-lxc.ps1 : installer-lxc.sh <dossier reçu> <destination> <adresse> <port> <avec données 0|1>
set -euo pipefail
src=$1 dest=$2 bind=$3 port=$4 with_data=$5

mkdir -p "$dest"
# Les fichiers venus de Windows n'ont pas de droits Unix fiables : on les fixe.
install -o root -g root -m 644 "$src"/app/* "$src"/app/.dockerignore "$dest/"
chown root:root "$dest"
chmod 755 "$dest"

if [ "$with_data" = 1 ]; then
    if [ -d "$dest/data" ] && [ -n "$(ls -A "$dest/data")" ]; then
        backup="$dest/data.avant-$(date +%Y%m%d-%H%M%S)"
        mv "$dest/data" "$backup"
        echo "Anciennes données mises de côté : $backup"
    fi
    cp -r "$src/data" "$dest/data"
fi

# Données personnelles : lisibles uniquement par l'utilisateur du conteneur (uid 10001)
mkdir -p "$dest/data"
chown -R 10001:10001 "$dest/data"
chmod 700 "$dest/data"
find "$dest/data" -type f -exec chmod 600 {} +

printf 'BMW_BIND=%s\nBMW_PORT=%s\n' "$bind" "$port" > "$dest/.env"
rm -rf "$src"

cd "$dest"
docker compose up -d --build
docker compose ps
