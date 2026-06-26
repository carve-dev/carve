with source as (
    select * from {{ source('shop', 'customers') }}
)

select
    customer_id,
    customer_name
from source
