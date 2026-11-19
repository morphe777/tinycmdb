-- Le matériel : ce que sont les machines, et pas seulement ce qu'elles font tourner.
--
-- Trois ajouts, pour qu'un agent qui aide à concevoir ou à dépanner sache ce que l'on a
-- réellement entre les mains, sans aller le chercher ailleurs :
--
-- - sur le nœud, l'architecture, le processeur, la carte mère, la mémoire, et pour un
--   invité Proxmox sa configuration complète ;
-- - une table `cpu`, référentiel des modèles de processeur : les caractéristiques d'un
--   modèle ne changent pas, elles se saisissent une fois et servent à toutes les machines
--   qui le portent ;
-- - une table `composant` : cartes PCIe, disques, périphériques USB, découverts sur les
--   hyperviseurs ou saisis à la main, avec la VM à laquelle ils sont attribués.

CREATE TABLE cpu (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    vendor         INTEGER REFERENCES option(id) ON DELETE SET NULL,
    arch           INTEGER REFERENCES option(id) ON DELETE SET NULL,
    generation     TEXT,
    cores          INTEGER,
    threads        INTEGER,
    base_mhz       INTEGER,
    boost_mhz      INTEGER,
    tdp_w          INTEGER,
    l3_mb          INTEGER,
    x86_level      INTEGER REFERENCES option(id) ON DELETE SET NULL,
    igpu           TEXT,
    virtualization TEXT,
    max_ram_gb     INTEGER,
    doc            TEXT,
    notes          TEXT
);

ALTER TABLE node ADD COLUMN arch INTEGER REFERENCES option(id) ON DELETE SET NULL;
ALTER TABLE node ADD COLUMN cpu_id INTEGER REFERENCES cpu(id) ON DELETE SET NULL;
ALTER TABLE node ADD COLUMN sockets INTEGER;
ALTER TABLE node ADD COLUMN cpu_type TEXT;
ALTER TABLE node ADD COLUMN cpu_level INTEGER REFERENCES option(id) ON DELETE SET NULL;
ALTER TABLE node ADD COLUMN machine TEXT;
ALTER TABLE node ADD COLUMN kernel TEXT;
ALTER TABLE node ADD COLUMN motherboard TEXT;
ALTER TABLE node ADD COLUMN bios TEXT;
ALTER TABLE node ADD COLUMN ram_type INTEGER REFERENCES option(id) ON DELETE SET NULL;
ALTER TABLE node ADD COLUMN ram_ecc INTEGER REFERENCES option(id) ON DELETE SET NULL;
ALTER TABLE node ADD COLUMN ram_mhz INTEGER;
ALTER TABLE node ADD COLUMN ram_modules TEXT;
ALTER TABLE node ADD COLUMN ram_max_gb INTEGER;
ALTER TABLE node ADD COLUMN config TEXT;

CREATE INDEX node_cpu ON node(cpu_id);

CREATE TABLE composant (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL UNIQUE,
    name         TEXT,
    category     INTEGER REFERENCES option(id) ON DELETE SET NULL,
    bus          INTEGER REFERENCES option(id) ON DELETE SET NULL,
    host_id      INTEGER REFERENCES node(id) ON DELETE SET NULL,
    assigned_id  INTEGER REFERENCES node(id) ON DELETE SET NULL,
    vendor       TEXT,
    model        TEXT,
    reference    TEXT,
    chipset      TEXT,
    address      TEXT,
    iommu_group  INTEGER,
    serial       TEXT,
    ports        INTEGER,
    speed_mbps   INTEGER,
    capacity_gb  INTEGER,
    standard     INTEGER REFERENCES option(id) ON DELETE SET NULL,
    usage        TEXT,
    health       TEXT,
    wearout      INTEGER,
    firmware     TEXT,
    doc          TEXT,
    source       INTEGER REFERENCES option(id) ON DELETE SET NULL,
    last_seen    TEXT,
    notes        TEXT
);

CREATE INDEX composant_host ON composant(host_id);
CREATE INDEX composant_assigned ON composant(assigned_id);

CREATE TRIGGER cpu_menage AFTER DELETE ON cpu BEGIN
    DELETE FROM choix WHERE tab = 'cpu' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'cpu');
END;
CREATE TRIGGER composant_menage AFTER DELETE ON composant BEGIN
    DELETE FROM choix WHERE tab = 'composant' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'composant');
END;

-- Le vocabulaire de l'architecture est celui de Docker (`linux/amd64`, `linux/arm64`…) :
-- c'est lui qui permet de rapprocher un hôte des plateformes qu'une image publie.
INSERT OR IGNORE INTO option (tab, champ, valeur, couleur, ordre) VALUES
    ('node', 'Arch', 'amd64', 'blue', 1), ('node', 'Arch', 'arm64', 'green', 2),
    ('node', 'Arch', 'arm/v7', 'yellow', 3), ('node', 'Arch', '386', 'gray', 4),
    ('node', 'Arch', 'riscv64', 'purple', 5),
    ('node', 'CPU_level', 'x86-64-v1', 'gray', 1), ('node', 'CPU_level', 'x86-64-v2', 'yellow', 2),
    ('node', 'CPU_level', 'x86-64-v3', 'green', 3), ('node', 'CPU_level', 'x86-64-v4', 'blue', 4),
    ('node', 'RAM_type', 'DDR3', 'gray', 1), ('node', 'RAM_type', 'DDR4', 'blue', 2),
    ('node', 'RAM_type', 'DDR5', 'green', 3), ('node', 'RAM_type', 'LPDDR4', 'cyan', 4),
    ('node', 'RAM_type', 'LPDDR4X', 'cyan', 5), ('node', 'RAM_type', 'LPDDR5', 'cyan', 6),
    ('node', 'RAM_ECC', 'ECC', 'green', 1), ('node', 'RAM_ECC', 'Non-ECC', 'gray', 2),
    ('cpu', 'Vendor', 'Intel', 'blue', 1), ('cpu', 'Vendor', 'AMD', 'red', 2),
    ('cpu', 'Vendor', 'ARM', 'green', 3), ('cpu', 'Vendor', 'Broadcom', 'purple', 4),
    ('cpu', 'Vendor', 'Rockchip', 'orange', 5), ('cpu', 'Vendor', 'Apple', 'gray', 6),
    ('cpu', 'Arch', 'amd64', 'blue', 1), ('cpu', 'Arch', 'arm64', 'green', 2),
    ('cpu', 'Arch', 'arm/v7', 'yellow', 3), ('cpu', 'Arch', '386', 'gray', 4),
    ('cpu', 'Arch', 'riscv64', 'purple', 5),
    ('cpu', 'x86_level', 'x86-64-v1', 'gray', 1), ('cpu', 'x86_level', 'x86-64-v2', 'yellow', 2),
    ('cpu', 'x86_level', 'x86-64-v3', 'green', 3), ('cpu', 'x86_level', 'x86-64-v4', 'blue', 4),
    ('composant', 'Category', 'Network', 'blue', 1),
    ('composant', 'Category', 'Wireless', 'cyan', 2),
    ('composant', 'Category', 'Disk', 'green', 3),
    ('composant', 'Category', 'Storage controller', 'yellow', 4),
    ('composant', 'Category', 'GPU', 'purple', 5),
    ('composant', 'Category', 'Accelerator', 'pink', 6),
    ('composant', 'Category', 'USB controller', 'orange', 7),
    ('composant', 'Category', 'USB device', 'orange', 8),
    ('composant', 'Category', 'Other', 'gray', 9),
    ('composant', 'Bus', 'PCIe', 'blue', 1), ('composant', 'Bus', 'NVMe', 'green', 2),
    ('composant', 'Bus', 'SATA', 'yellow', 3), ('composant', 'Bus', 'SAS', 'orange', 4),
    ('composant', 'Bus', 'USB', 'purple', 5), ('composant', 'Bus', 'Other', 'gray', 6),
    ('composant', 'Standard', '1GbE', 'gray', 1), ('composant', 'Standard', '2.5GbE', 'blue', 2),
    ('composant', 'Standard', '10GbE', 'blue', 3), ('composant', 'Standard', 'SFP+', 'blue', 4),
    ('composant', 'Standard', 'Wi-Fi 6', 'cyan', 5), ('composant', 'Standard', 'Wi-Fi 6E', 'cyan', 6),
    ('composant', 'Standard', 'Wi-Fi 7', 'cyan', 7), ('composant', 'Standard', 'Bluetooth 5', 'cyan', 8),
    ('composant', 'Standard', 'USB 2.0', 'orange', 9), ('composant', 'Standard', 'USB 3.0', 'orange', 10),
    ('composant', 'Standard', 'USB 3.1', 'orange', 11), ('composant', 'Standard', 'USB 3.2', 'orange', 12),
    ('composant', 'Standard', 'PCIe 3.0', 'purple', 13), ('composant', 'Standard', 'PCIe 4.0', 'purple', 14),
    ('composant', 'Standard', 'SATA III', 'yellow', 15),
    ('composant', 'Standard', 'Zigbee', 'green', 16), ('composant', 'Standard', 'Z-Wave', 'green', 17),
    ('composant', 'Standard', 'LoRa', 'green', 18),
    ('composant', 'Source', 'Auto', 'blue', 1), ('composant', 'Source', 'Manual', 'green', 2);
