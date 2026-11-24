-- Les unités de calcul graphique et les accélérateurs : ce qu'il faut savoir pour décider
-- où faire tourner un modèle de langue, un transcodage ou une détection d'objets.
--
-- Un GPU reste un composant, comme le collecteur le fait déjà pour l'iGPU Intel d'un
-- hyperviseur : une ligne par unité, rattachée à sa machine, attribuable à une VM. Une
-- machine à quatre cartes a quatre lignes ; un Mac Mini en a une pour son GPU et une pour
-- son Neural Engine (catégorie Accelerator).
--
-- Ce qui manquait pour les décrire :
--
-- - la mémoire qu'il a à sa disposition, et si elle est à lui ou partagée avec le
--   processeur — c'est elle qui borne la taille du modèle qu'on peut charger, et sur une
--   puce Apple elle se confond avec la mémoire de la machine ;
-- - le nombre de cœurs graphiques, qui distingue deux déclinaisons d'une même puce ;
-- - les interfaces de calcul qu'il accepte : une application qui veut CUDA ne tourne pas
--   sur ROCm, et c'est la première question qu'on se pose ;
-- - un bus « SoC », pour un GPU qui n'est relié à rien de visible : le classer en PCIe
--   serait faux, et « Other » ne dirait rien.

ALTER TABLE composant ADD COLUMN vram_gb INTEGER;
ALTER TABLE composant ADD COLUMN shared_memory INTEGER;
ALTER TABLE composant ADD COLUMN gpu_cores INTEGER;

-- « Other » reste en dernier, sauf si l'ordre a déjà été changé dans l'administration.
UPDATE option SET ordre = 7
 WHERE tab = 'composant' AND champ = 'Bus' AND valeur = 'Other' AND ordre = 6;

INSERT OR IGNORE INTO option (tab, champ, valeur, couleur, ordre) VALUES
    ('composant', 'Bus', 'SoC', 'cyan', 6),
    ('composant', 'Compute_API', 'CUDA', 'green', 1),
    ('composant', 'Compute_API', 'ROCm', 'red', 2),
    ('composant', 'Compute_API', 'oneAPI', 'blue', 3),
    ('composant', 'Compute_API', 'Metal', 'gray', 4),
    ('composant', 'Compute_API', 'Vulkan', 'purple', 5),
    ('composant', 'Compute_API', 'OpenCL', 'yellow', 6),
    ('composant', 'Compute_API', 'VA-API', 'orange', 7),
    ('composant', 'Compute_API', 'QSV', 'blue', 8),
    ('composant', 'Compute_API', 'NVENC', 'green', 9),
    ('composant', 'Compute_API', 'Edge TPU', 'pink', 10),
    ('composant', 'Compute_API', 'Hailo', 'pink', 11),
    ('composant', 'Compute_API', 'Core ML', 'gray', 12);
