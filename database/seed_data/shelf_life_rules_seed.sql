-- USDA FSIS FoodKeeper 데이터 기반 보관기간 시드.
-- database/seed_shelf_life_seed.py가 자동 생성함 — 직접 수정하지 말고 스크립트와 ENTRIES를 고칠 것.
BEGIN;

-- 우유 <- FoodKeeper: Milk (ultra-pasteurized)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('우유', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 30, 90, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 두부 <- FoodKeeper: Tofu
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('두부', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 계란 <- FoodKeeper: Eggs (in shell)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('계란', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 21, 35, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 애호박 <- FoodKeeper: Zucchini (fresh, whole)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('애호박', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 대파 <- FoodKeeper: Onions (spring or green)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('대파', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 양파 <- FoodKeeper: Onions (yellow, white, red, etc.)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('양파', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 60, 60, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 돼지고기 <- FoodKeeper: Pork (loin chops, boneless)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('돼지고기', 'FROZEN', 'UNOPENED', 'PURCHASE_DATE', 120, 360, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 닭고기 <- FoodKeeper: Chicken (whole)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('닭고기', 'FROZEN', 'UNOPENED', 'PURCHASE_DATE', 360, 360, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 소고기 <- FoodKeeper: Beef (steaks)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('소고기', 'FROZEN', 'UNOPENED', 'PURCHASE_DATE', 120, 360, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 새우 <- FoodKeeper: Shrimp, crayfish
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('새우', 'FROZEN', 'UNOPENED', 'PURCHASE_DATE', 180, 540, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 양배추 <- FoodKeeper: Cabbage
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('양배추', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 14, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 오이 <- FoodKeeper: Cucumbers
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('오이', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 4, 6, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 마늘 <- FoodKeeper: Garlic
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('마늘', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 3, 14, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 감자 <- FoodKeeper: Potatoes
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('감자', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 14, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 당근 <- FoodKeeper: Carrots, parsnips
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('당근', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 14, 21, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 콩나물 <- FoodKeeper: Bean sprouts
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('콩나물', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 5, 10, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 요거트 <- FoodKeeper: Yogurt
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('요거트', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 14, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 버터 <- FoodKeeper: Butter
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('버터', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 30, 60, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 치즈 <- FoodKeeper: Cheese (hard such as cheddar, swiss, block parmesan)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('치즈', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 180, 180, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 식빵 <- FoodKeeper: Bread (homemade)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('식빵', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 60, 90, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 상추 <- FoodKeeper: Lettuce (iceberg, romaine)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('상추', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 14, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 시금치 <- FoodKeeper: Lettuce (leaf, spinach)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('시금치', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 3, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 버섯 <- FoodKeeper: Mushrooms
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('버섯', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 3, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 햄 <- FoodKeeper: Ham (fully cooked, slices, half, or spiral cut)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('햄', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 3, 4, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 소시지 <- FoodKeeper: Sausage (fully cooked smoked links, kielbasa)
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('소시지', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 베이컨 <- FoodKeeper: Bacon
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('베이컨', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 7, 7, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 딸기 <- FoodKeeper: Strawberries
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('딸기', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 2, 3, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 사과 <- FoodKeeper: Apples
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('사과', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 28, 42, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

-- 바나나 <- FoodKeeper: Bananas
INSERT INTO receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state, reference_date_type, duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) VALUES ('바나나', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 3, 3, 'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data', '2025-01-22', CURRENT_TIMESTAMP);

COMMIT;