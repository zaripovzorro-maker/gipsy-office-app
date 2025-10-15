from __future__ import annotations
from google.cloud import firestore
from datetime import datetime

db = firestore.Client()  # использует GOOGLE_APPLICATION_CREDENTIALS

def upsert(col, doc, data):
    ref = db.collection(col).document(doc)
    ref.set(data, merge=True)

def main():
    # inventory
    upsert("inventory","espresso_beans",{"name":"Зёрна эспрессо","unit":"g","capacity":5000,"current":3000,"updated_at":datetime.utcnow()})
    upsert("inventory","milk",          {"name":"Молоко","unit":"ml","capacity":10000,"current":6000,"updated_at":datetime.utcnow()})
    upsert("inventory","syrup_vanilla", {"name":"Сироп ваниль","unit":"ml","capacity":2000,"current":800,"updated_at":datetime.utcnow()})
    upsert("inventory","sugar",         {"name":"Сахар","unit":"g","capacity":3000,"current":1200,"updated_at":datetime.utcnow()})
    upsert("inventory","cups_200",      {"name":"Стаканы 200 мл","unit":"pcs","capacity":500,"current":300,"updated_at":datetime.utcnow()})
    upsert("inventory","cups_400",      {"name":"Стаканы 400 мл","unit":"pcs","capacity":500,"current":200,"updated_at":datetime.utcnow()})

    # recipes
    upsert("recipes","cappuccino_base",{
        "base_volume_ml": 200,
        "ingredients":[
            {"ingredient_id":"milk","qty":150,"unit":"ml"},
            {"ingredient_id":"espresso_beans","qty":10,"unit":"g"},
            {"ingredient_id":"cups_200","qty":1,"unit":"pcs"}
        ]
    })
    upsert("recipes","latte_base",{
        "base_volume_ml": 200,
        "ingredients":[
            {"ingredient_id":"milk","qty":170,"unit":"ml"},
            {"ingredient_id":"espresso_beans","qty":8,"unit":"g"},
            {"ingredient_id":"cups_200","qty":1,"unit":"pcs"}
        ]
    })

    # products
    upsert("products","cappuccino",{
        "name":"Капучино",
        "category":"Кофе",
        "volumes":[200,400],
        "base_price":18000,
        "addons":[
            {"id":"syrup_vanilla","name":"Ванильный сироп","price_delta":2000,"ingredients":{"syrup_vanilla":20}}
        ],
        "recipe_ref":"recipes/cappuccino_base",
        "is_active": True
    })
    upsert("products","latte",{
        "name":"Латте",
        "category":"Кофе",
        "volumes":[200,400],
        "base_price":17000,
        "addons":[
            {"id":"syrup_vanilla","name":"Ванильный сироп","price_delta":2000,"ingredients":{"syrup_vanilla":20}}
        ],
        "recipe_ref":"recipes/latte_base",
        "is_active": True
    })

    print("✅ Seed done")

if __name__ == "__main__":
    main()

