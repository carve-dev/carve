with source as (
    select * from {{ source('shop', 'orders') }}
)

select
    order_id,
    customer_id,
    order_total
from source
